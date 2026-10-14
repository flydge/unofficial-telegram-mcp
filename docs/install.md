# Unofficial Telegram MCP: installation and configuration

[← Overview](../README.md) · [Tool reference](reference.md) · [Security](security.md)

**Existing installations:** the product was renamed from TelegramSearch. Keep your current installation folder and MCP configuration. The package, commands, skill identifier and local data paths retain their existing names for compatibility; new checkouts use `unofficial-telegram-mcp`.

## Private bundled candidate 0.52.0.dev0

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
The installer registers only its owned `mcp_servers.telegram_search` subtree
and the paired skills under `~/.agents/skills`. It preserves neighboring TOML
and refuses foreign registrations, skills, runtime roots and existing services.
Recovery retains one lease, journal, original config bytes and exact receipts;
repeating the same admitted installation verifies/reuses it. It performs no
login, Keychain operation, service start/stop or session mutation.

Initialization first prepares a complete private sibling packet and one bounded
adjacent `.TelegramSearch.install-intent.json` record before publishing the
runtime root. Earlier interrupted packets remain preserved for recovery; their
bytes alone never authorize adoption. Later publications bind exact staged
filesystem objects before rename or replacement. The runtime's parent directory
must already exist safely; the default macOS Application Support parent is used.

Installation returns `activation_required` and nonsecret `activation_command` and `activation_arguments`.
Run that exact installed Python/isolated launcher command ending in `setup`
from a foreground, uncaptured Apple Terminal. `resume` recovers a managed
interrupted enrollment; `status` reads bounded enrollment/handshake metadata.
The launcher uses `-I -S -B` and a closed app path. Credentials, phone, login
code and optional 2FA use hidden native prompts only. After confirmed native
teardown and owner handoff, require the matching broker's fresh Ready, reconnect
Codex, check the installed manifest and perform one authorized bounded own-chat
search. Synthetic installation and an inert manifest do not close that gate.


### Native activation and recovery

Use the exact `activation_command` returned by the authenticated installer;
its installed launcher accepts `setup`, `resume` and `status`. Run setup or
resume yourself in foreground, uncaptured native Apple Terminal. Native
Keychain, Telegram, launchd and clean-user acceptance remain open.

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
state; a local checkpoint cannot prove Ready. QR, email, registration and
purchase authorization states are unsupported and return a safe status without
provider details. Setup has a bounded 120-second auth/input deadline; another
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

The example lists all 43 tools and sets the required timeouts. Start a fresh chat in that project after configuration or schema changes.

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

The containing directory must be owned by you with mode `0700`; the regular policy file must have mode `0600`. Symlinks are rejected. Preserve existing policy when upgrading rather than overwriting it blindly.

| To enable | Add these capabilities |
| --- | --- |
| Selected messages and history | `read_messages`, `read_history`, `read_reply_chain` |
| Topics and richer search | `list_topics`, `read_topic_history`, `search_messages`, `list_chats`, `search_chats` |
| Reuse a verified chat | `verified_targets` |
| Document pages, spreadsheets, presentations | `attachment_pages`, `spreadsheets`, `presentations` |
| Approved sending | `send` |
| Exact forum-topic text sends/replies | `send`, `topic_text_send`, and `reply_text_send` for replies |
| Explicit outgoing text styles | `send`, `formatted_text_send`, and `reply_text_send` for replies |
| Text or attachment replies | `send` plus `reply_text_send` or `reply_artifact_send` respectively |

Replying to media or formatted sources can require additional source capabilities. Use the [reply reference](reference.md#prepared-same-chat-text-replies) for the exact combinations; do not enable every capability as a shortcut.

After a policy change, re-run `.venv/bin/telegram-search-broker install` using the same environment and reconnect your MCP client. This restarts the managed broker and clears in-memory drafts, uploads and cursors.

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
