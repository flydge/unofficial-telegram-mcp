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
   come from the reviewed trust input (`/bin/sh -p packaging/bootstrap.sh …`, see
   docs/install.md "Private bundled candidate 0.52.0.dev0"; the installer prints its JSON
   result, and this skill is itself installed by that installer). Keep installer failures and transaction evidence. Refuse
   foreign runtime, registration, service or skill conflicts; never stop an existing owner to
   make an install pass. Success is `status: activation_required` with a nonsecret
   `activation_command`. `claude_code` is `registered` or `reused` when Claude Code has the
   entry (Codex is registered either way). `claude_skills: skipped` means no Claude Code was
   found, so no copies went to `~/.claude/skills`.
2. **Connect.** Run `activation_command` exactly as returned; it ends in `connect`. It is safe in
   a captured shell, returns within seconds (up to about 40 s while a broker starts) and never
   prints the page address.
3. **Wait.** Run the same command with `wait` in place of `connect`. It returns at the next
   phase change, or after 9 minutes at most, so give the shell call a 10-minute timeout (Claude
   Code Bash: `timeout: 600000`; the default would cut it at 2 minutes). Codex needs the same:
   its `tool_timeout_sec` does not apply to shell commands, so use Codex's own long-running
   command support. Act on `next_action` (table below) and repeat `wait` until the status is
   `ready`, `failed`, `expired` or `cancelled`. The first `wait` usually returns
   `waiting_for_scan` within seconds: that is the moment to tell the user to scan.
4. **On `ready`** (or `already_connected`), ask the user to reconnect the MCP client: Claude Code
   `/mcp`, Codex restart. If `/mcp` does not list `telegram_search` after an install made during
   this Claude Code session, the user restarts Claude Code. Then call
   `_manifest(check_broker=true)` and require `compatible`. Finish with one bounded,
   user-chosen own-chat read or search using the paired telegram-search skill.

`cancel` (same command, `cancel` in place of `connect`) closes a screen that `connect` opened:
`cancelled`, or `nothing_to_cancel` when no background screen is running.

## Results by next_action

Never retry in a loop: run `connect` at most twice for the same failure, then stop and report.

| next_action | Status or code | What to do |
|---|---|---|
| `wait` | `screen_opened`, `connect_in_progress`, `starting`, `authorizing`, `handing_off`, `cancel_failed` | Run `wait`. Never start a second `connect` while one is in progress. After `cancel_failed` the screen closes by itself within 10 minutes. |
| `tell_user_to_scan_qr_then_wait` | `waiting_for_scan` | Tell the user a browser window opened on this Mac: in Telegram on the phone open Settings > Devices > Link Desktop Device and scan the QR. Then `wait`. |
| `tell_user_to_enter_password_on_page_then_wait` | `waiting_for_password` | The account has two-step verification: the user types that password on the page, never in this chat. Then `wait`. |
| `scan_qr_on_this_mac_screen` | `waiting_for_scan` returned after a full 9-minute `wait` | Ask whether the user sees the page. If yes, `wait` again. If not (headless, SSH, no browser), run `cancel`, then use the Terminal fallback. |
| `none` | `ready` | Step 4. `already_connected` (no next_action) means the same. |
| `wait_then_connect_again` | `broker_starting` | The user's own broker is still starting Telegram. Wait about 30 s, then `connect` again. |
| `wait_retry_after_then_connect_again` | `flood_wait` | Telegram asked to wait `retry_after` seconds (a few minutes when it is null). Tell the user; `connect` only after that. |
| `run_connect_again` | `expired`, `cancelled`, `invalid_password`, `invalid_input`, `password_submission_uncertain`, `auth_request_failed`, `timeout`, `dependency_unavailable`, `auth_close_failed`; `owner_busy` after this run restarted the user's broker | Offer to run `connect` again. |
| `run_connect_in_native_terminal` | `connect_launch_failed`, `helper_exited`, `broker_start_failed`, `broker_readiness_timeout`, `broker_incompatible` | Ask the user to run the same command with `connect` in native Apple Terminal (it shows the real result there) and to tell you when it ends; logging out and in also restarts a stopped broker. |
| `use_terminal_setup_fallback` | `unsupported_auth_state`, `app_identity_missing` | Use the Terminal fallback. For `app_identity_missing` the user may instead store their own API pair with `api-credentials` in native Terminal, then `connect` again. |
| `run_api_credentials_in_native_terminal` | `credential_conflict`; any `api-credentials` refusal not listed elsewhere | Only one of the two Keychain API records exists. The user runs the same command with `api-credentials` in native Terminal (add-only; you never see it), then `connect` again. |
| `unlock_keychain_then_connect_again` | `credential_denied`, `credential_locked` | The user unlocks their login Keychain or allows the access prompt, then `connect` again. |
| `repair_keychain_record_manually` | `credential_invalid_record` | A Keychain record is malformed; add-only writes cannot repair it. The user inspects it in Keychain Access; never read or delete it for them. |
| `check_existing_broker_without_stopping_it` | `owner_busy` | Something else holds the Telegram session. Never stop it. Ask the user to finish or close it, then retry once. |
| `preserve_existing_session_and_use_legacy_install_path` | `legacy_unmanaged` | An unmanaged session exists and stays untouched. Stop and say so. |
| `review_enrollment_binding_without_resetting`, `restore_exact_managed_key_without_regenerating`, `stop_and_report` | `enrollment_inconsistent`, `identity_changed`, `managed_key_missing`, `enrollment_missing`, `app_identity_invalid`, `policy_unsafe`, `owner_unsafe`, `credential_unavailable` | Stop and report the code. Never reset, regenerate or delete anything to get past it. |
| `exit_connect_process_then_run_connect_again` | `auth_destroy_failed` | Let the connect process exit (close that Terminal run), then `connect` again. |
| `run_in_foreground_native_terminal` | `secure_terminal_required` | `setup`, `resume` and `api-credentials` run only in the user's own foreground native Terminal. |
| `run_connect` | `enrollment_exists`, `connect_helper_requires_launchd` | Use `connect`. `connect-helper` is internal; never run it. |

Installer results:

| Result | What to do |
|---|---|
| `claude_code`: `failed`, `unavailable` or `not_found` | Claude Code is not registered. Offer the returned `claude_registration_command` (an argv list; quote each element as one shell word) for the user to run in their own terminal, then `/mcp`. If it says `telegram_search` already exists (for example the 0.51 manual `claude mcp add`), the user removes it themselves with `claude mcp remove telegram_search -s user` and runs the command again. |
| `claude_registration_conflict` | Nothing was installed: Claude Code has a different `telegram_search` entry. The user removes it themselves with `claude mcp remove telegram_search -s user`, then install again. Never remove it for them. |
| `recovery_limit`, any other installer code, `bundle_admission_or_install_failed`, `bundle_admission_failed`, `invalid_arguments` | Stop and report it with the retained transaction evidence; do not retry. |
| `api-credentials`: `stored` or `already_present` | The pair is in the Keychain (nothing was overwritten). Continue with `connect`. |

## Terminal fallback

The same launcher accepts `setup` (phone, code and 2FA through hidden prompts), `resume`,
`api-credentials` and `status`. Only the user, in a foreground uncaptured native Apple
Terminal, runs `setup`, `resume` and `api-credentials`. `status` is read-only. A screen still
open from `connect` must be closed first with `cancel`.

## Boundaries

Installation, a started page or a launch is never Ready; only `ready` followed by a compatible
`_manifest` is. The preserved default session lives under
`~/Library/Application Support/TelegramSearchMCP/tdlib` and is never overwritten, purged or
revoked from here. The default policy (written by `connect`) enables only `read` and `artifacts`; `send`,
`forward_send` and `video_send` need a manual `runtime.toml` edit and a broker restart (docs/install.md,
"Enabling more capabilities"). There is no account
alias or profile registry, cloud auth or model provisioning route. Source and redistribution
completeness and public hosting remain open gates; do not call this candidate released or
download missing runtime, source, media tools or models without authority.
