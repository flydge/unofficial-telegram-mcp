---
name: telegram-setup
description: Install the local Unofficial Telegram MCP bundle and connect the user's Telegram account with a QR code scanned on their phone, without ever handling credentials.
---

# Telegram setup

Paired with unreleased candidate `0.52.0.dev0`, contract 1. Needs Apple Silicon macOS 26+
and Claude Code or Codex running on the same Mac as the user's browser and Telegram phone app.

**Never ask for, echo, store or pass on:** API ID or hash, phone number, login code, 2FA
password, QR link or the connection-screen address. No command below takes any of them. If a
step seems to need one, stop and use the Terminal fallback at the end.

## Flow

1. **Install** only from an independently authenticated bundle index and seed whose digests
   come from the reviewed trust input. Keep installer failures and transaction evidence. Refuse
   foreign runtime, registration, service or skill conflicts (including
   `claude_registration_conflict`); never stop an existing owner to make an install pass.
   Success is `status: activation_required` with a nonsecret `activation_command`.
   `claude_code` says whether the Claude Code entry was `registered`, `reused` or `not_found`
   (no Claude Code CLI at its usual places; tell the user, Codex is still registered).
2. **Connect.** Run `activation_command` exactly as returned; it ends in `connect`. It is safe in
   a captured shell, returns at once and never prints the page address.
3. **Tell the user** a browser window opened on this Mac: in Telegram on their phone open
   Settings > Devices > Link Desktop Device and scan the QR. If they use two-step verification,
   they type that password on the page, never in this chat.
4. **Wait.** Run the same command with `wait` in place of `connect`. One call blocks up to
   9 minutes, so give the shell call a 10-minute timeout (Claude Code Bash: `timeout: 600000`;
   the default would cut it at 2 minutes). Repeat while the status is not terminal. Terminal statuses are `ready`, `failed`,
   `expired` and `cancelled`; act on the returned `code` and `next_action`.
5. **On `ready`**, ask the user to reconnect the MCP client: Claude Code `/mcp`, Codex restart.
   Then call `_manifest(check_broker=true)` and require `compatible`. Finish with one bounded,
   user-chosen own-chat read or search using the paired telegram-search skill.

## Other results

| Result | What to do |
|---|---|
| `already_connected` | Skip to step 5. |
| `connect_in_progress` | A page is already open. Do not start another; go to step 4. |
| `connect_launch_failed` | The background job could not start. Ask the user to run the same command in native Apple Terminal and tell you when it ends, then check with `wait`. |
| `owner_busy` | Something else holds the Telegram session. Never stop it. Ask the user to finish or close it, then retry. |
| `legacy_unmanaged` | An existing unmanaged session is present and is left untouched. Stop and say so. |
| `app_identity_missing` | This build carries no application identity. The user may store their own API pair by running the same command with `api-credentials` in native Terminal (you never see it) and then `connect` again, or use the Terminal fallback. |
| `waiting_for_scan` that persists | The page may not have opened (headless, SSH, no browser). Ask whether the user sees it; if not, use the Terminal fallback. |
| `expired`, `cancelled`, `helper_exited` | Offer to run `connect` again. |
| `unsupported_auth_state` | Use the Terminal fallback. |
| any other `failed` code | Follow its `next_action`; do not retry in a loop. |

## Terminal fallback

The same launcher accepts `setup` (phone, code and 2FA through hidden prompts), `resume`,
`api-credentials` and `status`. Only the user, in a foreground uncaptured native Apple
Terminal, runs `setup`, `resume` and `api-credentials`. `status` is read-only.

## Boundaries

Installation, a started page or a launch is never Ready; only `ready` followed by a compatible
`_manifest` is. The preserved default session lives under
`~/Library/Application Support/TelegramSearchMCP/tdlib` and is never overwritten, purged or
revoked from here. The default policy enables only `read` and `artifacts`. There is no account
alias or profile registry, cloud auth or model provisioning route. Source and redistribution
completeness and public hosting remain open gates; do not call this candidate released or
download missing runtime, source, media tools or models without authority.
