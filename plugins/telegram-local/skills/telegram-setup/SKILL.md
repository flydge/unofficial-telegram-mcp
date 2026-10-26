---
name: telegram-setup
description: Install the local Unofficial Telegram MCP bundle and connect the user's Telegram account with a QR code scanned on their phone, without ever handling credentials.
---

# Telegram setup

Paired with candidate `0.59.0rc2`, contract 1. Needs Apple Silicon macOS 26+
and Claude Code, Codex or Claude Desktop running on the same Mac as the user's browser and Telegram
phone app. The signed DMG and the public release assets do not exist yet, so the routes below
describe what the installer does; confirm with the user which kit they have.

**Never ask for, echo, store or pass on:** API ID or hash, phone number, login code, 2FA
password, QR link or the connection-screen address. No command below takes any of them. If a
step seems to need one, stop and use the Terminal fallback at the end.

## Flow

1. **Install.** Pick the route that fits the client. Every route installs from an
   independently authenticated bundle index and seed whose digests come from the reviewed trust
   input, and ends in the installer's JSON result (see docs/install.md "Install from the 0.53 kit").
   - **Claude Desktop, or any client without a shell for you:** ask the user to open the DMG and
     double-click `Install.command`. It installs and then runs `connect` inline in the user's own
     Terminal: the QR page opens there and the final result prints there, so no `wait` and no pasted
     command are needed. When the user says it finished, ask them to quit and reopen Claude Desktop
     (it reads its configuration only at launch), then go to step 4.
   - **Claude Code plugin** (`telegram-local`, added with `/plugin marketplace add` and
     `/plugin install`): the plugin carries no runtime until it is installed, and its server stops with
     `runtime_not_installed`. First read `<plugin root>/release-pins.json`, where `<plugin root>` is the
     directory the plugin is installed in (`${CLAUDE_PLUGIN_ROOT}`), and show the user its `version` and
     `team_id`; continue only when `team_id` is the publisher Team ID `256X5MUA6S` from the project
     README. With the user's go-ahead, run `/bin/sh -p "<plugin root>/bin/install"` with no arguments;
     if you cannot tell what the plugin root is, ask the user to run the command instead of guessing a
     path. It prints the installer result with
     `claude_code: plugin`. Continue at step 2 (over an installed release it upgrades instead; see
     "Upgrade, rollback and uninstall").
   - **Shell route** (`/bin/sh -p bootstrap.sh …` from a kit folder, with `--claude-code user`, `plugin` or
     `skip`): the installer prints its JSON result. Continue at step 2.

   Keep installer failures and transaction evidence. Refuse foreign runtime, registration, service or
   skill conflicts; never stop an existing owner to make an install pass, and never use the installer's
   legacy-adoption option (docs/install.md, "Adopting entries the installer does not own") unless the
   owner asks for that migration. Success is `status: activation_required` with
   a nonsecret `activation_command`. `claude_code` is `registered` or `reused` when Claude Code has the
   entry (Codex is registered either way), or `plugin` or `skipped` in those modes. `claude_skills:
   skipped` means no copies went to `~/.claude/skills` (no Claude Code found, or the plugin supplies
   them). `claude_desktop: registered` comes with `claude_desktop_next_action:
   restart_claude_desktop`.
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
   `/mcp`, Codex restart, Claude Desktop quit and reopen. If `/mcp` does not list `telegram_search`
   (or the plugin's `tg` server) after an install made during
   this Claude Code session, the user restarts Claude Code. Then call
   `_manifest(check_broker=true)` and require `compatible`. Finish with one bounded,
   user-chosen own-chat read or search using the paired telegram-search skill.

`cancel` (same command, `cancel` in place of `connect`) closes a screen that `connect` opened:
`cancelled`, or `nothing_to_cancel` when no background screen is running.

**An existing account alias.** When the server that needs connecting is `telegram_search_<alias>` (its
instructions name the alias), run steps 2 to 4 with `--account <alias>` after `connect`, `wait` and
`cancel`. A plain `connect` serves only the default account and reports `already_connected` while the
alias stays offline.

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
| `review_enrollment_binding_without_resetting`, `stop_and_report` | `account_duplicate`, `account_mismatch`, `alias_unavailable` | On any login once an alias registry exists (from the first `account-add` on), the default account's included (with `--account <alias>` or without). `account_duplicate`: the Telegram account that just logged in (QR scan, `setup` or `resume`) already serves another alias or the default account. `account_mismatch`: it is not the account this alias, or the default, was first connected with. `alias_unavailable`: the alias is no longer registered, or its account directory is missing (only `account-add` creates it); on the default's login, the alias registry cannot be read. Nothing was handed to the broker; stop and tell the user which account was connecting (the alias, or the default). Never remove an alias or reset a session to get past it yourself. The owner's ways out for the default: for `account_duplicate`, `account-remove` the alias that holds that Telegram account, then `connect` again; for `account_mismatch`, log the default into the account it was first connected with (its pin stays until no alias is left and an uninstall retires the registry). |
| `finish_pending_installer_action_then_retry`, `upgrade_installation_or_remove_account`, `run_account_add_for_alias_then_retry` | `transaction_pending`, `newer_state_kept`, `older_state_kept` | Only with account aliases on an installed release; nothing changed. `transaction_pending`: a first install, upgrade, rollback, uninstall, `account-add` or `account-remove` is unfinished; `pending_action` names it, and `pending_account` the alias of an account action. The user runs that installer action again, then `connect` again (with no `pending_action`, stop and report). `newer_state_kept`: this alias's state is a newer release's, which this release cannot serve: a rollback kept it on the release its session was made with (it stays offline until the next upgrade, then `connect` again), or a lower version was installed after an uninstall kept it (the user installs that release again, then `account-add <alias>`); or its `account-remove`. `older_state_kept`: an uninstall kept this alias and a newer release was installed since; run `account-add <alias>` (it repairs the alias and keeps its session), then `connect --account <alias>` again. After a reinstall at a newer version, run `account-add <alias>` for each alias the uninstall kept before any `connect` or upgrade: both refuse until then. After a reinstall at a lower version, upgrade to exactly the release the alias last ran, then `account-add <alias>`. Never edit its files or reset its session to get past it. |
| `exit_connect_process_then_run_connect_again` | `auth_destroy_failed` | Let the connect process exit (close that Terminal run), then `connect` again. |
| `run_in_foreground_native_terminal` | `secure_terminal_required` | `setup`, `resume` and `api-credentials` run only in the user's own foreground native Terminal. |
| `run_connect` | `enrollment_exists`, `connect_helper_requires_launchd` | Use `connect`. `connect-helper` is internal; never run it. |

Installer results:

| Result | What to do |
|---|---|
| `claude_code`: `failed`, `unavailable` or `not_found` | Claude Code is not registered. Offer the returned `claude_registration_command` (an argv list; quote each element as one shell word) for the user to run in their own terminal, then `/mcp`. If it says `telegram_search` already exists (for example the 0.51 manual `claude mcp add`), the user removes it themselves with `claude mcp remove telegram_search -s user` and runs the command again. |
| `claude_registration_conflict` | Nothing was installed: Claude Code has a different `telegram_search` entry. The user removes it themselves with `claude mcp remove telegram_search -s user`, then install again. Never remove it for them. |
| `claude_code_user_entry`: `present` (plugin mode) | An older user-scope entry would list the tools twice. The user removes it with `claude mcp remove telegram_search -s user`; the installer never does. |
| `claude_desktop`: `registered` | Ask the user to quit and reopen Claude Desktop (`claude_desktop_next_action: restart_claude_desktop`). `reused` needs nothing; `skipped` means Claude Desktop is not installed. |
| `claude_desktop`: `failed` or `unavailable` | Claude Desktop is not registered. Show the returned `claude_desktop_manual_entry` (nonsecret JSON) for the user to merge into `claude_desktop_config.json` themselves, then restart Claude Desktop. Never edit that file for them. |
| `claude_desktop_registration_conflict` | Nothing was installed: Claude Desktop has a different `telegram_search` entry (for example the 0.51 manual one). The user removes or renames it themselves, then installs again. |
| `claude_desktop_config_unsupported` | Nothing was installed: that config cannot be rewritten without changing a neighbour. Stop and report it; the user can add the entry by hand. |
| `release_not_published`, `pins_invalid`, `pin_mismatch`, `download_failed`, `bootstrap_failed`, `result_invalid` (plugin `bin/install`); `activation_unexpected`, `activation_failed`, `kit_incomplete` (`Install.command`) | Stop and report the code; do not retry in a loop. `release_not_published` means no release exists yet for this plugin version. |
| `recovery_limit`, any other installer code, `bundle_admission_or_install_failed`, `bundle_admission_failed`, `invalid_arguments` | Stop and report it with the retained transaction evidence; do not retry. |
| `api-credentials`: `stored` or `already_present` | The pair is in the Keychain (nothing was overwritten). Continue with `connect`. |

## Upgrade, rollback and uninstall

Only when the user asks, and best in the user's own native Terminal. Before it stops the broker, and
before it changes any client entry, the installer asks the broker whether a send is in flight, an
approval dialog is open, a send outcome is not yet settled, or any client context is connected (an open
MCP client can count as one). It then refuses with `pending_send_unresolved`: no
client entry, broker or session has changed, but an upgrade or rollback may stay unfinished, and then
other actions refuse with `transaction_pending` until the same command runs again. Ask the user to settle
any open dialog and any `outcome_unknown` send (`get_send_status`), quit every MCP client (or leave the
Telegram tools unused for about five minutes) and run the same command again; stop after two refusals and tell the user that this same command finishes it later.

- **Upgrade:** the user runs the newer kit's `Install.command`, the same `bootstrap.sh` command with the
  newer pins (without `--claude-code`, or with the installed mode), or in plugin mode a newer plugin's
  `bin/install`. Each keeps the installed mode and prints `status: upgraded` (`installation: reused` on a
  repeat), which is a success. `Install.command` then runs `connect` in the same window; with a ready
  session it reports `already_connected`. Every client then reconnects.
- **Rollback or uninstall:** the installed release's launcher with `rollback` or `uninstall` in place of
  `connect`. `RELEASE` is the `release_root` in
  `~/Library/Application Support/TelegramSearch/current.json`, for example
  `RELEASE=$(/usr/bin/plutil -extract release_root raw -o - "$HOME/Library/Application Support/TelegramSearch/current.json")`,
  then `"$RELEASE/python/bin/python3" -I -S -B "$RELEASE/launcher.py" rollback`. Rollback returns to the
  release before the last upgrade and refuses (`rollback_refused`) once newer session state exists.
  Uninstall removes only what the installer owns; the Telegram session, enrollment and Keychain stay.
- `downgrade_refused`, `upgrade_conflict`, `policy_conflict`, `rollback_unavailable`, `service_conflict`,
  `service_restart_failed`, `uninstall_incomplete`, `installation_refused`: stop and report the code.
  `login_in_progress`: the login of the alias in `account` still runs; let it end (`wait --account <account>`
  or `cancel --account <account>`), then the same command again. `connect_lock_unsafe`: that alias's
  `run/connect.lock` (or its `run` folder or root) is not owner-only, which `wait` and `cancel` cannot clear;
  stop and report the code and `account` (the user restores it: `chmod 700` the folders, `chmod 600` the
  lock). `alias_broker_unreadable`: a leftover account
  broker of a removed release; the user runs the returned `bootout_command` themselves, then the same
  command again.
  `transaction_pending`: an earlier install, upgrade, rollback, uninstall, `account-add` or
  `account-remove` is unfinished, and `pending_action` names it (`pending_account` the alias of an account
  action); repeat that command first. An uninstall interrupted after it renamed the
  installation folder is repeated with the launcher inside
  `~/Library/Application Support/.TelegramSearch-removed-<random>/releases/<sha256>/`. Never edit
  `runtime.toml`, the broker plist or a registration to get past a refusal.
- `target_conflict`, `skill_conflict`, `policy_conflict` or `foreign_registration` from an upgrade, a
  rollback or a first install that adopts an older installation: the installer's own `telegram_search`
  entry, skill copy or `runtime.toml` version line changed while it ran. When that comes after the broker
  stop, the installer has already put the old broker back (its `runtime.toml` version line and plist as
  they were, started again if it was running), so the installed release keeps serving; the change stays
  unfinished, and clients whose entry was already switched start the server only once the same command
  finishes. Tell the user which object changed; the user puts it back or removes it themselves (a
  removed `telegram_search` table or skill copy is written again), then runs the same command, as often
  as needed. Edits elsewhere in those files are kept and need nothing. For an upgrade or rollback the
  other way out is `uninstall`, which also removes what the change already wrote; `rollback` and another
  kit refuse with `transaction_pending` until the same command or `uninstall` has run. An adopting first
  install ends only with its own repeat.

## A second account

Only when the owner asks. The owner picks an alias: a lowercase letter, then at most 11 lowercase letters
or digits, never `default`. Run the installed launcher (`RELEASE` as for rollback above) with
`account-add <alias>`: it registers the alias, creates its account directory and client entries, never
logs in, and prints `account_added` with an `activation_command` ending in `connect --account <alias>`.
Then do steps 2 to 4 with `--account <alias>` after `connect`, `wait` and `cancel`; the user scans with
the phone of the account being added, and the page names the alias. It becomes its own server
`telegram_search_<alias>` (the plugin serves only the default account). `account_duplicate` or
`account_mismatch` from `wait` (table above) means that phone holds an account already connected, or not
this alias's own; stop and report. After an uninstall and a reinstall the same `account-add` re-registers
an alias the uninstall kept, with its session (`service: rewritten` is normal there); run it for each
kept alias before any `connect` or upgrade. `policy_conflict` there means a newer release served that
session. `alias_exists`, `registry_full`, `alias_path_too_long`,
`alias_name_too_long`, `accounts_inconsistent`, `release_not_current` or `policy_conflict`: stop and
report the code (`login_in_progress` and `connect_lock_unsafe` as above). `default_check: unanswered` is
not a failure: the default account is pinned at its broker's next check. `account-remove <alias>`
(renaming is remove, then add) runs only when the owner asks; it moves the session aside, never deleting
it.

## Terminal fallback

The same launcher accepts `setup` (phone, code and 2FA through hidden prompts), `resume`,
`api-credentials` and `status`. Only the user, in a foreground uncaptured native Apple
Terminal, runs `setup`, `resume` and `api-credentials`. `status` is read-only. A screen still
open from `connect` must be closed first with `cancel`.

## Boundaries

Installation, a started page or a launch is never Ready; only `ready` followed by a compatible
`_manifest` is. The preserved default session lives under
`~/Library/Application Support/TelegramSearchMCP/tdlib` and is never overwritten, purged or
revoked from here. The default policy (written by `connect`) enables only `read` and `artifacts`. Only the
owner turns more on, in their own Terminal, with the installed launcher's `capabilities enable <profile>`
(`readers`, `send`, `replies`, `forward`, `video`, `archive_edit`, `albums`, `pin`, `delete`; `show` lists
them, `disable` turns one off; docs/install.md, "Enabling more capabilities"). Never run it yourself, even with a
shell, and never present it as a step in the middle of a session: it refuses (`pending_send_unresolved`)
while a send is in flight, an approval dialog is open, a send outcome is not yet settled, or any MCP client
context is active (up to 300 s after the last Telegram call). Tell the owner to settle pending sends, quit or
disconnect every MCP client (or wait five minutes), run it, then reconnect. `service_conflict`, `policy_conflict`
and `policy_unsupported` change nothing: stop and report the code. `status: changed` with
`broker: restart_failed` changed the policy: the owner runs the returned `restart_command`. Never edit the
policy yourself. There is no cloud auth or
model provisioning route. Source and redistribution
completeness and public hosting remain open gates; do not call this candidate released or
download missing runtime, source, media tools or models without authority. The plugin's pinned
`bin/install` is the one download this skill runs, only for a user who chose the plugin, and it
verifies every asset before anything runs.
