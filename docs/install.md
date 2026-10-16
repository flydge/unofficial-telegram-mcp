# Unofficial Telegram MCP: installation and configuration

[← Overview](../README.md) · [Tool reference](reference.md) · [Security](security.md)

**Existing installations:** the product was renamed from TelegramSearch. Keep your current installation folder and MCP configuration. The package, commands, skill identifier and local data paths retain their existing names for compatibility; new checkouts use `unofficial-telegram-mcp`.

## Private bundled candidate 0.52.0.dev0

**Current behavior versus the 0.52 target:** the approved
[0.52 QR-first requirement](plans/0.52-qr-onboarding.md) is the primary path of this
candidate: install → `connect` → local QR screen → phone confirmation → optional 2FA →
ready MCP, with no API ID/hash, phone number or login code going through your
assistant. It is implemented and covered by synthetic and inert-fake tests only. Real
acceptance (a clean macOS user, a test-account phone scan with 2FA, a bounded search in
Claude Code and in Codex, restart and revoke/reconnect) is still pending, so nothing
here is released or accepted end to end. The phone/code Terminal flow remains as the
[fallback](#terminal-fallback-phone-and-code-setup).

The continuing development candidate supports an offline authenticated first
install for Apple Silicon macOS 26+. Its bundle supplies standalone Python,
the exact runtime dependencies, pinned TDLib/OpenSSL and paired search/setup
skills. It requires no consumer Python, Homebrew, Git or compiler. This is an
unreleased private installation route; published 0.51 instructions below remain
historical published support. Public hosting, publisher trust, source completeness
and clean-user/native onboarding acceptance remain open.

From native Terminal, run the reviewed local `packaging/bootstrap.sh` with
`/bin/sh -p`, absolute `--seed`, independently supplied `--seed-sha256` and
`--seed-bytes`, absolute `--archive` and `--index`, and independently supplied
`--trusted-index-sha256`. The trust pins must come from the reviewed release
input, not from an untrusted archive. `--prepare-only` authenticates and extracts
into a retained private stage without installing. There is no public runnable
download URL in this candidate.

Default runtime storage is `~/Library/Application Support/TelegramSearch`,
separate from the preserved default session in `TelegramSearchMCP/tdlib`.
The installer registers, with ownership receipts:

- Codex: only its owned `mcp_servers.telegram_search` subtree in `~/.codex/config.toml`;
  neighboring TOML is preserved.
- Claude Code: a user-scope server `telegram_search`. Its state is read only from the
  `mcpServers` object of `~/.claude.json` (a bounded, no-follow, strict-JSON read that never
  starts a process). A different `telegram_search` entry there (for example the 0.51
  manual `claude mcp add`) refuses the install as `claude_registration_conflict` before
  anything is created; remove it yourself with `claude mcp remove telegram_search -s user`.
  An identical entry is reused. Otherwise the entry is added once with
  `claude mcp add-json --scope user` and confirmed by re-reading the file. The `claude` CLI
  is looked up only at `~/.local/bin/claude`, `~/.claude/local/claude`,
  `/opt/homebrew/bin/claude` and `/usr/local/bin/claude`; every path component and link
  must be owned by you or root and not group/world-writable (`PATH` is never searched),
  and the resolved file runs with only `HOME`, `PATH=<its directory>:/usr/bin:/bin:/usr/sbin:/sbin`
  and `LC_ALL=C`. Claude Code registration never blocks the install otherwise: the result
  records `claude_code` as `registered`, `reused`, `failed`, `unavailable` (unreadable
  `~/.claude.json`) or `not_found` (no CLI), and for the last three returns a nonsecret
  `claude_registration_command` argv you can run yourself. The outcome is final for that
  installation; a repeat or resumed install never re-runs it. Only an entry the installer
  has just added, before its outcome is recorded, is ever removed on a refusal.
- The paired `telegram-search` and `telegram-setup` skills under `~/.agents/skills`, and
  under `~/.claude/skills` only when `~/.claude` already exists or the CLI was found
  (otherwise the receipt records `claude_skills: skipped`).

It refuses foreign registrations, skills, runtime roots and existing services.
A refused install prints only `{"status":"refused","code":...}` on stderr: the installer's
fixed code (for example `claude_registration_conflict` or `recovery_limit`), or
`bundle_admission_or_install_failed` for anything else; other launcher actions refuse as
`bundle_admission_failed`. Recovery retains one lease, journal, original config bytes and exact receipts;
repeating the same admitted installation verifies/reuses it, without consulting the
`claude` CLI again. Apart from those registration commands it performs no login,
Keychain operation, service start/stop or session mutation. A launch checks the release
tree, index, receipt, pointer, transaction journal and home binding, and only the shape of
the client records. It never reads the Codex `config.toml`, the Claude registration or the
skill copies in `~/.agents/skills` and `~/.claude/skills`, so editing or wiping client
state cannot stop the installed runtime from starting; a repeat install verifies the Codex
registration and the `~/.agents/skills` copies and refuses drift there.

Initialization first prepares a complete private sibling packet and one bounded
adjacent `.TelegramSearch.install-intent.json` record before publishing the
runtime root. Earlier interrupted packets remain preserved for recovery; their
bytes alone never authorize adoption. Later publications bind exact staged
filesystem objects before rename or replacement. The runtime's parent directory
must already exist safely; the default macOS Application Support parent is used.

Installation returns `status: activation_required`, `next_action: run_activation_then_wait`,
`claude_code`, `claude_skills` and the nonsecret
`activation_command` and `activation_arguments`: the installed Python and isolated
launcher (`-I -S -B`, a closed app path) ending in `connect`. Installation, a started
screen or a launch is not Ready. The installer prints this JSON result in Terminal; paste
the nonsecret `activation_command` to your assistant.


### Connect with a QR code (primary path)

Your assistant (Claude Code or Codex) can run all of this from its captured shell; no
step takes a secret.

1. Run the exact `activation_command`. `connect` validates the installation, starts a
   short-lived background job outside the assistant's process tree and returns at once with
   `screen_opened`. The job opens your default browser on this Mac at a local
   `http://127.0.0.1:<port>/` page; the address is never printed or logged.
2. In Telegram on your phone open **Settings → Devices → Link Desktop Device** and scan the
   QR. It refreshes by itself and shows a countdown. If you use two-step verification, type
   the password on that page only (never to the assistant). The page is in Russian or English
   following your browser language.
3. Your assistant runs the same command with `wait` in place of `connect`. One call returns
   at the next phase change (the first one usually reports `waiting_for_scan` within
   seconds), or after at most 540 seconds, with the phase and a `next_action`; it repeats
   until `ready`, `failed`, `expired` or `cancelled`. Give the shell call a timeout of at
   least 10 minutes (Claude Code's default Bash timeout is 2 minutes). The whole run lives at most 600 seconds,
   counted on the wall clock, so a Mac that sleeps does not extend it. `cancel` closes a
   screen that `connect` opened (the background job is stopped and tears its temporary
   Telegram client down first) and reports `cancelled` or `nothing_to_cancel`.
4. On `ready`, reconnect the MCP client (Claude Code: `/mcp`, and if `/mcp` does not list
   `telegram_search` after an install made during this Claude Code session, restart Claude
   Code; Codex: restart it), have the
   assistant call `_manifest` with `check_broker=true`, and make one bounded own-chat
   read or search. That, not the installer or `ready` alone, is the end-to-end check.

Repeating `connect` is safe: with a Ready broker it reports `already_connected` and asks for
no new login, and while a screen is open it reports `connect_in_progress`. If the broker is
the exact receipt-bound one but its Telegram authorization is no longer valid (for example
after revoking the session from the phone), `connect` may stop that one broker, run the QR
flow on the existing managed enrollment and hand off again; a foreign, incompatible or Ready
owner is never stopped. An existing unmanaged session is never touched (`legacy_unmanaged`).
A new policy enables only `read` and `artifacts`; see
[Enabling more capabilities](#enabling-more-capabilities-candidate).

If the broker answers but its first check (which starts TDLib and re-verifies the bundle) is
still running after 25 seconds, `connect` reports `broker_starting` with
`next_action: wait_then_connect_again` and stops nothing. If Telegram rate-limits a 2FA
attempt, the result is `flood_wait` with `retry_after` in seconds (the page shows the wait in
minutes); connect again only after that. Every code maps to one fixed `next_action`, the
same in `wait`, the inline result and refusals.

If the background job cannot start, or the assistant's sandbox refuses to create the files
`connect` needs (`connect_launch_failed`), if the job ends without reporting a result
(`helper_exited`), or no page can open (headless, SSH: run `cancel` first), run the same
command from a native Apple Terminal window on this Mac, or use the Terminal fallback below.
In native Terminal, `connect` runs the helper inline and prints the final result when the run
ends (up to 600 seconds); no `wait` is needed there.

**Application identity.** With no API pair of your own, `connect` uses the application
identity shipped in the bundle (`app-identity.json`, owner-only and hash-authenticated like
every bundled file). Credential order for a new enrollment: a complete API pair already in
your Keychain, else the bundled identity, else `connect` refuses `app_identity_missing`.
A partial Keychain pair is `credential_conflict`; an existing enrollment keeps the source and
identity it was created with (`identity_changed` otherwise). To use your own pair (obtain it
from [Telegram's application portal](https://my.telegram.org/apps)), run the launcher with
`api-credentials` in a native Terminal before the first `connect`; it prompts with hidden
input and stores the pair add-only.

**Assembling a bundle with the identity.** The bundle producer adds the identity only when
asked: `python packaging/assemble_bundle.py --payload <dir> --output-directory <dir>
--app-identity-from-keychain`. It reads `api_id` and `api_hash` from the build machine's
Keychain service, validates them, and writes `app-identity.json` (canonical JSON, mode 0400)
at the release root before the index is computed, so it is indexed like every other file.
Values are never printed and errors are redacted. Without the flag no such file exists, and a
payload that already carries one is refused.

**Risk note.** Every user of a bundle shares the distributor's API ID, and distributed client
parameters are extractable by design. If Telegram were to restrict that application (for
example under §1.5 of its API terms), every bundled-identity installation, and the owner's own
installation if it uses the same pair, would stop working. The Keychain override above is the
mitigation. This was accepted by the owner on 2026-10-14 and is not legal sign-off.


### Enabling more capabilities (candidate)

`connect` writes `~/Library/Application Support/TelegramSearchMCP/runtime.toml` (mode `0600` in
a `0700` directory) with `expected_package_version = "0.52.0.dev0"`,
`expected_contract_version = 1` and `enabled_capabilities = ["read", "artifacts"]`: `send`,
`forward_send`, `video_send`, `archive_chats`, `edit_own_text` and every extended reader are off. To enable more, edit that file
yourself. Keep the pins as written and add only the capabilities you need from the
[capability table below](#published-051-choose-capabilities) (for example `"send"` plus
`"forward_send"`, `"video_send"`, `"archive_chats"` or `"edit_own_text"`). The bundle has no `telegram-search-broker install`. Check
pending send outcomes first, then restart the broker so it loads the new policy:

```bash
launchctl kickstart -k gui/$(id -u)/com.<home-directory-name>.telegram-search-mcp.broker
```

`<home-directory-name>` is the last component of your home directory. Running components
report `config_stale` until restarted. Restarting the broker clears in-memory drafts, uploads
and cursors, so reconnect the MCP client afterwards (Claude Code: `/mcp`; Codex: restart it).

### Terminal fallback: phone and code setup

The same launcher accepts `setup`, `resume`, `api-credentials` and `status` besides `connect`,
`wait` and `cancel`. Run setup, resume and api-credentials yourself in foreground, uncaptured native
Apple Terminal; `resume` recovers a managed interrupted enrollment, `status` reads bounded
enrollment/handshake metadata. After confirmed native teardown and owner handoff, require the
matching broker's fresh Ready, reconnect the client, check the installed manifest and perform
one authorized bounded own-chat search. Synthetic installation and an inert manifest do not
close that gate. Native Keychain, Telegram, launchd and clean-user acceptance remain open.

Setup refuses agent/captured shells, SSH, tmux, script wrappers and redirected or
piped input before credentials or session changes. It uses hidden prompts for
missing API credentials, your phone number, the Telegram code and optional 2FA
password. Obtain your own API pair through [Telegram's application portal](https://my.telegram.org/apps).
It preserves valid existing `api_id`/`api_hash` records and adds only missing
records through Security.framework. A denied or locked Keychain is a recoverable
error, never treated as an absent record or a reason to overwrite one.

Only an absent or genuinely empty default session can enroll. Any unrecognized
nonempty legacy session returns `legacy_unmanaged` and remains untouched.
A private `enrollment.json` record and session-side `.managed-enrollment` identity
marker bind the session path, directory identities, OS owner and dedicated
`database_key_<enrollment-id>` Keychain account. A random 32-byte key encrypts
only this new session. Missing, damaged or rebound metadata/key refuses; setup
never regenerates a managed key or changes an existing database's encryption.

Cancellation or a failed/uncertain submission retains progress and attempts
temporary-client close/destroy without logout, revocation or deletion. Use
`resume` after addressing the reported issue. Before enrollment, resume can
complete add-only partial API provisioning or retry cancelled credential entry
under the same genuinely-new-session checks; it cannot adopt legacy or damaged
managed state. Resume asks TDLib for fresh authorization
state; a local checkpoint cannot prove Ready. In this Terminal flow, QR, email, registration
and purchase authorization states are unsupported (QR is handled by `connect`) and return a
safe status without provider details. Setup has a bounded 120-second auth/input deadline; another
resume may be needed if Telegram's code arrives later.

Provider Closed and native destruction are reported separately. If destruction
is uncertain (`auth_destroy_failed`), the owning setup process retains its lease
and auth owner until process exit. Allow that invocation to exit before resuming;
an embedded caller must exit its owning process first. Destruction is never
retried automatically, and no broker handoff occurs in that state. A provider
close error with confirmed destruction releases ownership but still reports
failure instead of Ready.

The temporary auth client holds the broker's exact owner lease through native
close/destroy. After destruction and lease release, setup can create an absent
credential-free matching LaunchAgent definition and bootstrap it. It preserves
an existing plist/registration and refuses a mismatch. It never stops a competing
owner. A contended owner must first pass read-only validation of the intended
installed plist/live registration. Success requires both the matching broker contract/config handshake and
fresh TDLib Ready; launch success alone, a timeout or `owner_busy` is not readiness.
Status reads metadata and an existing handshake only, without Keychain, provider
initialization or service startup; `checkpoint: ready` is a past observation.

A new policy enables only `read` and `artifacts`. An existing policy stays
byte-identical; no send or extended capability is silently enabled. Preserve the exact
managed key and both metadata bindings for recovery: **older binaries using an
empty key cannot open a newly encrypted managed session**. This rollback boundary
does not migrate or alter legacy databases.


## Published 0.51: prepare the legacy runtime

The published 0.51 package runs on **Apple Silicon macOS** with **Python 3.11–3.14**. It uses an existing, dedicated Telegram session; it does not include a first-time login wizard or a reproducible TDLib provisioning script.

Prepare these locally before installing the broker:

| Requirement | Exact expectation |
| --- | --- |
| TDLib | Homebrew keg `HEAD-d1085f9`, library `1.8.67`, legacy `td_json_client_*` ABI. `/opt/homebrew/opt/tdlib/lib/libtdjson.dylib` must resolve into that keg. A current unpinned `brew install tdlib` is not equivalent. |
| Telegram API credentials | Your own `api_id` and `api_hash`, stored as generic-password items in macOS Keychain. Use service `com.<home-directory-name>.telegram-search-mcp`, with separate accounts `api_id` and `api_hash`. |
| Authorized session | A dedicated TDLib session under `~/Library/Application Support/TelegramSearchMCP/tdlib`, using its `db` and `files` subdirectories, compatible with this runtime and ready for `authorizationStateReady`. |

The service name uses the last component of your home directory, not your display name. Enter credentials through trusted local provisioning; never place them in project files, prompts, MCP arguments or shell history. The broker only reads Keychain values. It cannot ask for your phone number, login code or two-step password.

A logged-in Telegram Desktop app does **not** supply this TDLib session. If you do not already have compatible provisioning, stop here: first-time setup is a published 0.51 product gap, not something the package-install commands below solve. Do not copy an unrelated application's live session files.

**Optional media dependencies:** local transcription and frame extraction use `/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/ffprobe` and `/opt/homebrew/bin/whisper-cli`. Provision the multilingual model at `~/Library/Application Support/TelegramSearchMCP/models/ggml-small.bin` and verify its distributor's checksum. Basic text search and document reads do not need Whisper. See the [security model](security.md) for details.

## Published 0.51: install the package and broker

**Version scope:** the commands below pin the `v0.51.0` source release. Confirm `telegram_search_mcp.__version__` before installing or changing an existing broker. The runtime-policy example below is for 0.51.0 only. See [release notes](release-notes-0.51.0.md) for accepted scenarios and limitations, and [licensing](licensing.md) for the AGPLv3 decision.

Use a stable, owner-only checkout. Select a supported Python interpreter if your `python3` is outside the supported range.

```bash
git clone --branch v0.51.0 https://github.com/flydge/unofficial-telegram-mcp.git
cd unofficial-telegram-mcp
chmod 700 .
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p "$HOME/Library/LaunchAgents"
.venv/bin/telegram-search-broker install
.venv/bin/telegram-search-mcp --check
```

`install` registers the background broker in your logged-in macOS session. The broker alone owns TDLib; MCP clients connect to it. Keep the checkout and virtual environment at the same path.

The check prints `INITIALIZING_TDLIB` followed by `AUTHORIZATION_READY` to stderr and exits `0` on success. It initializes the session when needed but does not search or read messages. Exit `2` means authorization is blocked; exit `1` indicates another setup, transport or compatibility failure.

## Published 0.51: connect an MCP client

### Codex

Merge the server block from [`.codex/config.toml.example`](../.codex/config.toml.example) into `.codex/config.toml` in the project where you want to use Unofficial Telegram MCP. Preserve any existing settings. Replace **both** absolute-path placeholders. The example does not expand `~` or shell variables.

The current candidate example lists all 55 tools and sets the required timeouts; published 0.51 consumers must use their matching manifest. Start a fresh chat in that project after configuration or schema changes.

Install the optional skill by copying [the skill file](../skills/telegram-search/SKILL.md) to `~/.agents/skills/telegram-search/SKILL.md`. Back up an existing copy first. The skill supplies search and approval guidance; the broker enforces its own permissions.

### Other local MCP clients

Choose a **STDIO** server with:

```json
{
  "command": "/ABSOLUTE/PATH/TO/unofficial-telegram-mcp/.venv/bin/telegram-search-mcp",
  "args": []
}
```

Use your client's configuration format and allow up to 600 seconds for bounded media operations. This release does not expose an HTTP endpoint. Start with `_manifest({"check_broker": true})` and verify compatibility before using the other tools.

## Published 0.51: choose capabilities

The client tool list controls what is visible. The broker's local policy controls what is allowed. Enabling a tool in a client does not enable its capability.

Without a policy file, the legacy defaults are `read`, `artifacts` and `send`; sending still requires the exact preview and local approval. Extended readers and reply features are opt-in.

Before changing policy, check any pending send outcomes. A policy edit makes running components stale, and a broker restart loses in-memory send status. Never repeat an uncertain send automatically.

For a read-only starting configuration, create or carefully edit:

```text
~/Library/Application Support/TelegramSearchMCP/runtime.toml
```

```toml
config_version = 1
expected_package_version = "0.51.0"
expected_contract_version = 1
enabled_capabilities = ["read", "artifacts"]
```

This pin is for 0.51.0 only; a candidate policy written by `connect` pins `0.52.0.dev0`.

The containing directory must be owned by you with mode `0700`; the regular policy file must have mode `0600`. Symlinks are rejected. Preserve existing policy when upgrading rather than overwriting it blindly.

| To enable | Add these capabilities |
| --- | --- |
| Selected messages and history | `read_messages`, `read_history`, `read_reply_chain` |
| Topics and richer search | `list_topics`, `read_topic_history`, `search_messages`, `list_chats`, `search_chats` |
| Reuse a verified chat | `verified_targets` |
| Retain exact messages or owned files (candidate) | `retained_selections`; original reader/send capabilities still apply |
| Document pages, spreadsheets, presentations | `attachment_pages`, `spreadsheets`, `presentations` |
| Approved sending | `send` |
| Exact forum-topic text sends/replies | `send`, `topic_text_send`, and `reply_text_send` for replies |
| Explicit outgoing text styles | `send`, `formatted_text_send`, and `reply_text_send` for replies |
| Text or attachment replies | `send` plus `reply_text_send` or `reply_artifact_send` respectively |
| Exact forward/copy of one message (candidate) | `send` and `forward_send`; see the [forward/copy reference](reference.md#exact-forwardcopy-f14). Off by default; each send still needs the preview, approval and local confirmation |
| One native MP4/MOV video of at most 64 MiB (candidate) | `send` and `video_send`, plus `artifacts` to supply the file; see the [native video reference](reference.md#native-video-send-f15a). Off by default; each send still needs the preview, approval and local confirmation |
| Move one chat between Main and Archive (candidate) | `send` and `archive_chats`; see the [archive and edit reference](reference.md#archiveunarchive-and-own-text-edit-f17a). Off by default; each move still needs the preview, approval and local confirmation. Also lets `prepare_chat_archive` reveal that chat's title, kind and list membership without a read capability |
| Edit the plain text of one own text message (candidate) | `send` and `edit_own_text`; see the [archive and edit reference](reference.md#archiveunarchive-and-own-text-edit-f17a). Off by default, independent of `archive_chats`; each edit still needs the preview, approval and local confirmation. Also lets `prepare_text_edit` reveal that message's text, formatting and edit date without a read capability |

Replying to media or formatted sources can require additional source capabilities. Use the [reply reference](reference.md#prepared-same-chat-text-replies) for the exact combinations; do not enable every capability as a shortcut.

On the published 0.51 route, after a policy change, re-run `.venv/bin/telegram-search-broker install` using the same environment and reconnect your MCP client. This restarts the managed broker and clears in-memory drafts, uploads and cursors. The 0.52 candidate bundle restarts the broker as described under [Enabling more capabilities](#enabling-more-capabilities-candidate).

## Published 0.51: troubleshooting

| Symptom | What to check |
| --- | --- |
| Pinned TDLib unavailable or wrong keg | Confirm the exact library path and pin above. |
| Missing credentials or blocked authorization | Check the local Keychain records and dedicated session; the package has no login flow. |
| `package_mismatch`, `schema_mismatch` or `config_mismatch` | Use matching package and policy in proxy and broker, restart the broker, then open a fresh client session. |
| A listed tool is disabled | Add its capability to trusted local policy and restart matching components. |
| Media analysis is unavailable | Check the fixed binary paths and locally provisioned Whisper model. |
| `status` succeeds but tools fail | Broker registration and Telegram authorization are separate; run `--check` and the manifest handshake. |

```bash
.venv/bin/telegram-search-broker status
.venv/bin/telegram-search-mcp --check
```

To stop and remove the managed service:

```bash
.venv/bin/telegram-search-broker uninstall
```

This removes the validated LaunchAgent, not your TDLib session or credentials. For upgrades, preserve the previous environment, configuration and installed skill so they can be restored together. See [rollback](security.md#rollback).
