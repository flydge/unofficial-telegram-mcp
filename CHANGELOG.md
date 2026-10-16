# Changelog

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
## 0.52.0.dev0 — unreleased

- Add F14 exact forward/copy of one message (development; [spec](docs/plans/0.53-forward-copy-spec.md)):
  tools `prepare_forward_send` and `send_prepared_forward` (49 tools) and the default-off
  `forward_send` capability, which also requires `send`. `mode="forward"` shows the original
  sender; `mode="copy"` sends a new message without attribution (`remove_caption` only for
  copied media). Preparation returns a complete preview with source and preview hashes, and
  refuses secret chats, hidden formatting and link targets, reply markup, auto-deleting,
  protected or non-forwardable sources and, in forward mode, forwarded sources. Sending
  revalidates recipient and source, asks the local owner to confirm the same facts and makes
  one closed-shape `forwardMessages` attempt with no retry; `get_send_status` reports the
  outcome. Re-preparing supersedes the earlier draft; the generic draft tools do not expose
  forward drafts. Verified with a synthetic provider only; real approved delivery is pending
  the owner. See [reference](docs/reference.md#exact-forwardcopy-f14) and
  [security](docs/security.md#exact-forwardcopy-boundary-f14-candidate).

- Add QR-first Telegram login to the private candidate: `connect` opens a loopback
  QR screen on the Mac, `wait` reports progress, 2FA is typed only on that page, and a
  bundle assembled with the application identity needs no API ID/hash entry. The installer now
  registers the MCP server in both Claude Code (user scope, through the `claude` CLI at
  fixed locations, foreign entries refused, `claude_code: not_found` when absent) and Codex,
  copies the paired skills to both `~/.agents/skills` and `~/.claude/skills`, and returns an
  activation command ending in `connect`. The bundle assembler injects `app-identity.json`
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
- Private installation ends at `activation_required`. Native onboarding,
  fresh Codex search, public distribution and source completeness remain open.

- Preserve client-owned artifact grants across the supported local lifecycle and
  add retained selections for bounded conversational message reuse.
- Add issuer-owned local upload authorities, stdin-only CLI handoff and original
  client result retrieval/cancellation, including idempotent receipt recovery after
  a lost response without copying bytes or renewing storage.
- Verify the bounded B2C slice with the full guarded suite, package/install checks,
  independent review and installed official SDK auto/legacy scenarios using
  synthetic data. No real Telegram send or full F18 acceptance is claimed.
- Pause development after B2C at the owner's request; retain the unfinished scope
  and evidence limits in [the roadmap](docs/roadmap.md). No new release is tagged.
