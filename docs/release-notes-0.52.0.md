# 0.52.0 — 2026-10-20

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This source release adds QR-first onboarding and three opt-in action slices to 0.51.0. It is an annotated
tag with release-gate evidence. The private authenticated bundle that carries an application identity is
**not distributed**. The [original plan](plans/initial-roadmap.md) is not finished; the
[completion plan](plans/completion-plan.md) and the [roadmap](roadmap.md) list what remains.

## Available behavior

- **QR-first onboarding.** `connect` opens a local page on the Mac (`127.0.0.1`, token in the URL
  fragment) that shows a QR code. The user scans it in Telegram on the phone and types an optional 2FA
  password only on that page. `wait` reports each phase, and `cancel` closes the screen. A revoked
  session is reconnected on the same enrollment. No API pair, phone number, login code or QR link passes
  through the assistant.
- **55 tools.** New tools, each behind its own default-off capability:
  - `prepare_forward_send` / `send_prepared_forward`: forward or copy exactly one message (`forward_send`);
  - `prepare_video_send` / `send_prepared_video`: one owned MP4/MOV of at most 64 MiB as a native video
    (`video_send`);
  - `prepare_chat_archive` / `apply_prepared_archive`: move one chat between Main and Archive
    (`archive_chats`);
  - `prepare_text_edit` / `apply_prepared_text_edit`: replace the plain text of one own message
    (`edit_own_text`).

  State-changing capabilities also require `send`; `archive_chats` and `edit_own_text` also require `read`,
  because their preparation reveals content. Each action has a complete preview, a local owner dialog and
  exactly one provider attempt, which is never retried.
- **The reviewed text is the stored text.** Text sends, replies, captions and edits apply Telegram's own
  rewrite before the preview: a tab becomes a space; U+2028, U+2029, U+030A, U+0333 and U+033F are
  removed; leading and trailing spaces and line feeds are trimmed. Style offsets refer to that final text.
- **Clearer refusals.** A tool whose capability is off returns a fixed reason naming the missing
  capabilities, for example `capability_disabled: read_history (owner must enable in runtime.toml)`.

Package version 0.52.0, public contract version 1, private IPC version 2. The new tools changed the schema
fingerprint. Deploy matching proxy, broker, policy and skills together. A policy written by `connect` pins
`expected_package_version = "0.52.0"`.

## Corrections included

- Keychain reads failed on every real Mac (`-50`); items are now counted first and exactly one item's data
  is read.
- A client request no longer restarts a broker that is still starting TDLib; it starts a stopped agent and
  waits up to 10 seconds.
- An archive draft is refused before any dialog when TDLib does not offer the target list, and a chat that
  a fresh session has not loaded yet is loaded once.

## Accepted scenarios and evidence chain

Real checks ran on the owner's Mac with source installs. Each row names the code it ran on and what changed
afterwards:

| Scenario (real account, real client) | Code | Later changes touching it |
| --- | --- | --- |
| QR login, handoff, repeat `connect` ⇒ `already_connected`, broker restart without login | b5a451e / a63f26b | archive pre-check, text normalization, guard texts, capability `read` for archive/edit; the connect and auth path is unchanged |
| Phone revoke ⇒ `AUTHORIZATION_BLOCKED`, page Cancel, QR refresh, screen expiry, reconnect, 2FA login, default policy `read`+`artifacts`, searches through the official SDK, `codex exec` and a new Claude Code session | ef90dba | text normalization, guard texts; the connect and auth path is unchanged |
| Approved copy and forward to Saved Messages, read back | a63f26b | text normalization (forward carries no new text) |
| Approved native video send with caption, read back | a63f26b | captions now get Telegram's rewrite before the preview |
| Approved own-text edit, read back | a63f26b | new text now gets Telegram's rewrite instead of being refused; `read` now required |
| Approved archive and unarchive of a supergroup | 94c6a80 | `read` now required |
| Guard refusal texts on the live install | 65f444b | version literals only |

**Not re-run after the text rewrite (0b29806):** the real sends and the edit above predate it. Its
behaviour is covered by unit, broker and transport-shape tests and by an independent review, but no real
send has used it yet.

## Known limits and unmet gate items

- **The 0.52 QR gate is not fully met.** The [requirement](plans/0.52-qr-onboarding.md) asks for a clean OS
  user without developer tools. That run was moved to the backlog by the owner on 20.10.2026. Unmet bullets:
  - installation through the bundle installer on a clean user;
  - a machine without developer Python, Homebrew, Git or a compiler;
  - interruption and resume (helper killed);
  - logout or reboot without a new login;
  - public users without their own API pair. A source install still needs the pinned Homebrew TDLib keg
    and the user's own API pair through `telegram-search-setup api-credentials`.
- The private bundle with the application identity is not distributed, signed or notarized.
- The F14 forward header was not checked visually in a Telegram app.
- A concurrent caller of one archive or edit draft can read `outcome_unknown`. A later call after lost
  authority on a forward or video draft reads `expired`, by the owner's decision.
- Multiple accounts, albums, the rest of message management and the rest of F13 remain on the roadmap.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, and the
compatibility identifiers `telegram-search-mcp`, `telegram_search`, the Python namespace, CLI names and
`TelegramSearchMCP` runtime paths are unchanged. A distinct product name is an open owner decision before
any public listing.
