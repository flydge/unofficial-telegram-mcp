# Unofficial Telegram MCP: installation and configuration

[← Overview](../README.md) · [Tool reference](reference.md) · [Security](security.md)

**Existing installations:** the product was renamed from TelegramSearch. Keep your current installation folder and MCP configuration. The package, commands, skill identifier and local data paths retain their existing names for compatibility; new checkouts use `unofficial-telegram-mcp`.

## Private bundle 0.53.0.dev0

**Current behavior versus the 0.52 target:** the approved
[0.52 QR-first requirement](plans/0.52-qr-onboarding.md) is the primary path of this
candidate: install → `connect` → local QR screen → phone confirmation → optional 2FA →
ready MCP, with no API ID/hash, phone number or login code going through your
assistant. It was accepted for real on the owner's Mac on 20.10.2026 (phone scan, 2FA,
QR refresh/cancel/expiry, revoke and reconnect, a bounded search in Claude Code and in
Codex). Installing the identity bundle on a clean macOS user is deferred to the backlog,
and the candidate is not released yet. The phone/code Terminal flow remains as the
[fallback](#terminal-fallback-phone-and-code-setup).

The continuing development candidate supports an offline authenticated first
install for Apple Silicon macOS 26+. Its bundle supplies standalone Python,
the exact runtime dependencies, pinned TDLib/OpenSSL and paired search/setup
skills. It requires no consumer Python, Homebrew, Git or compiler. This is an
unreleased private installation route; published 0.51 instructions below remain
historical published support. Public hosting, the Developer ID signed and notarized build (the
owner provisioned the identity and notary profile on 21.10.2026), source completeness and the
clean-user install remain open.

The 0.53 distribution path (a DMG with `Install.command`, the curl path, the Claude Code
plugin, Claude Desktop registration, upgrade, rollback and uninstall) is described in
[Install from the 0.53 kit](#install-from-the-053-kit-dmg-curl-and-plugin-development).
Its signed DMG and public release assets do not exist yet.

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
  neighboring TOML is preserved. Ownership covers that table's own keys and values (and comments
  between or on them); comments and blank lines after its last key, such as a comment you write above a
  later table, stay yours: they neither make the entry foreign nor change when the installer replaces
  or removes its lines.
- Claude Code (the default `--claude-code user`; `plugin` and `skip` are described in
  [the 0.53 section](#claude-code-user-scope-plugin-or-skip)): a user-scope server `telegram_search`. Its state is read only from the
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
- Claude Desktop: one `mcpServers.telegram_search` entry in
  `~/Library/Application Support/Claude/claude_desktop_config.json`, only when the `Claude`
  directory exists there; see [Claude Desktop](#claude-desktop).
- The paired `telegram-search` and `telegram-setup` skills under `~/.agents/skills`, and
  under `~/.claude/skills` only when `~/.claude` already exists or the CLI was found
  (otherwise the receipt records `claude_skills: skipped`).

A first install refuses foreign registrations, skills and runtime roots, and an existing broker plist
(`service_conflict`) unless [--adopt-legacy](#adopting-entries-the-installer-does-not-own---adopt-legacy)
is given. A refused install prints only `{"status":"refused","code":...}` on stderr: the installer's
fixed code (for example `claude_registration_conflict` or `recovery_limit`), or
`bundle_admission_or_install_failed` for anything else; `rollback` and `uninstall` refuse the same
way, and other launcher actions refuse as `bundle_admission_failed`. Recovery retains one lease, journal, original config bytes and exact receipts;
repeating the same admitted installation verifies/reuses it, without consulting the
`claude` CLI again. Apart from those registration commands a first install performs no login,
Keychain operation, service start or stop, or session mutation. The one exception is a first install
with `--adopt-legacy`: it stops the adopted broker (after the pending-send gate), starts it again if it
was running, and rewrites only the `expected_package_version` line of `runtime.toml`. An upgrade and a
rollback likewise stop only the receipt-bound (or adopted) broker, start it again only if it was
running, and rewrite only that line; `uninstall` stops only the receipt-bound broker and writes nothing
in the session directory. None of them logs in or touches the Keychain. A launch checks the release
tree, index, receipt, pointer, transaction journal and home binding, and only the shape of
the client records. It never reads the Codex `config.toml`, the Claude registration or the
skill copies in `~/.agents/skills` and `~/.claude/skills`, so editing or wiping client
state cannot stop the installed runtime from starting; a repeat install verifies the Codex
registration and the `~/.agents/skills` copies and refuses drift there.

Initialization first prepares a complete private sibling packet and one bounded
adjacent `.TelegramSearch.install-intent.json` record before publishing the
runtime root. A retry removes a killed attempt's packet only when every entry in it
is one the installer creates (an empty one only under one of its fixed attempt names);
anything else beside the root stays and is never adopted. Later publications bind exact staged
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
a `0700` directory) with `expected_package_version` set to the running package version
(`"0.53.0.dev0"` for this candidate),
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
close that gate. Native Keychain, Telegram and launchd behaviour was accepted on the owner's Mac on
20.10.2026; the clean-user install of the bundle is in the backlog.

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

Before upgrading from 0.51, keep copies of `enrollment.json` and `runtime.toml`. Once this
candidate writes the session state (setup, resume or `connect`), a pre-0.52 `enrollment.json`
gains `credential_source` and `identity_fingerprint`, and 0.51 binaries refuse it. A
`runtime.toml` naming any new capability (`forward_send`, `video_send`, `archive_chats`,
`edit_own_text`, …) is refused by 0.51 too. Roll back only with the preserved pre-upgrade files;
see [rollback](security.md#rollback).


## Install from the 0.53 kit: DMG, curl and plugin (development)

**Nothing in this section is published.** The signed DMG, the GitHub release and every public
release asset do not exist. The owner provisioned the Developer ID Application identity and the notary
profile on 21.10.2026 (the signing preflight returned `notary: ok`); the signed and notarized build,
its Gatekeeper observation and publication are owner-session rows
([owner checklist](plans/0.53-signing-owner-checklist.md)). The Claude Code plugin's
`release-pins.json` is the unreleased placeholder (all-zero hashes, `"publication": "unreleased"`),
so the plugin's `bin/install` refuses with `release_not_published` before any download, and the
marketplace entry exists only in the repository.

The text below describes what the committed scripts and installer do. Unit tests cover them with
synthetic kits. On the build Mac an ad hoc dry run built a kit, and `bootstrap.sh --prepare-only`
and `Install.command --prepare-only` from its mounted image both returned `verified: true`; that run
was recorded on 21.10.2026 (commits `c0f3cf3` and `a43985d`), before later changes to `bootstrap.sh`,
`Install.command`, the launcher and the installer, and has not been repeated since. A signed kit, a
clean macOS user, Claude Desktop and the plugin on a real machine, and the owner's migration are
owner-run rows that have not happened yet ([spec, phase G](plans/0.53-client-distribution-spec.md)).

### The ad hoc dry run versus the signed build

`packaging/release_build.py build --dry-run` (see section 5 of the owner checklist) builds the same
kit with the ad hoc identity `-`. It differs from the signed build in these ways:

- Nothing is notarized or stapled and the DMG is signed ad hoc, so Gatekeeper refuses that DMG on any
  other Mac. It is a test artifact for the build Mac.
- The kit's `release-pins.json` has `"team_id": ""` and `"publication": "unreleased"`. Its
  `Install.command` therefore carries no Team ID and does not pass `--seed-team`, and such pins never
  become the plugin's (`release_build.py release-commit` refuses a dry-run output).
- The seed and the payload's Mach-O files are signed ad hoc. The three TDLib/OpenSSL natives keep their
  pinned unsigned bytes. Passing `--seed-team` to `bootstrap.sh` with an ad hoc seed refuses as
  `seed_signature_invalid`.
- The manifest stays `publication: unreleased` and `source_completeness: incomplete`.

### What a kit contains

A kit is one folder. The DMG holds exactly that folder (the build compares the mounted image with
`SHA256SUMS`), under a volume named `Unofficial Telegram MCP`; both that name and the DMG file name
`<name>-<version>-macos-arm64.dmg` (currently `unofficial-telegram-mcp-…`) are placeholders until the
product has its own name.

| File | Purpose |
| --- | --- |
| `Install.command` | Generated for this kit from its `release-pins.json`. The seed and index pins, and a Developer ID kit's Team ID, are literals in the script. Not code-signed (see [DMG path](#dmg-path)). |
| `bootstrap.sh` | The reviewed entry script. |
| `bootstrap-verifier` | The native seed that authenticates the index and archive. |
| `release.tar.gz`, `release-index.json` | The bundle archive and the index it is checked against. |
| `release-pins.json` | The version, publication, Team ID, and the sha256 and size of `bootstrap.sh`, the seed, the index and the archive. The same schema, and in a release the same bytes, as the plugin's file (below). |
| `README.txt`, `LICENSE`, `NOTICE`, `third-party-notices.md` | Short instructions and licences. |
| `SHA256SUMS` | The sha256 of every other file. |

The release assets of the build would be the DMG, the five curl-path files (`bootstrap.sh`,
`bootstrap-verifier`, `release.tar.gz`, `release-index.json`, `release-pins.json`),
`release-provenance.json`, `corresponding-source-<version>.tar.gz` and a `SHA256SUMS` over them.

### Publisher Team ID and what binds what

The publisher's Developer ID Team ID is **`256X5MUA6S`** (also stated in the [README](../README.md)).
A signed release carries it on the DMG, the seed and every Mach-O of the payload.

- **The trust root** of the curl path is `release-pins.json` as committed in the repository at the
  release tag: `plugins/telegram-local/release-pins.json` at `v<version>`. The release build writes that
  file and the release commit copies it byte for byte ([releasing](releasing.md), section 4). The copy
  published next to the assets, or inside a kit, holds the same bytes but proves nothing: whoever can
  replace the assets can replace it too. The plugin path reads the plugin's own copy in your marketplace
  clone, which holds the same bytes only while the clone is at that tag; compare the two before
  installing ([Claude Code plugin](#claude-code-plugin), step 3). The plugin's scripts come from that same
  clone and no pin covers them, so for the plugin path the trust root is the repository branch the clone
  holds.
- **The pins** bind `bootstrap.sh`, the seed (`bootstrap-verifier`), `release-index.json` and
  `release.tar.gz` by sha256 and size, and name the Team ID. Compare `team_id` with `256X5MUA6S` as
  printed here, in a copy of this guide or the README you trust. The tag is only as trustworthy as the
  repository: anyone who can push to it can move a tag or change the file, and the Team ID comparison
  is the check that does not depend on where you downloaded from.
- **`bootstrap.sh`** checks the seed's sha256 and size and, with `--seed-team`, its Developer ID
  designated requirement for that Team ID before running it. The seed authenticates the index and every
  archive member before extraction; the launcher re-hashes the installed tree at every start.
- **On the DMG path** the DMG's own signature and stapled ticket cover the kit (`Install.command`,
  `bootstrap.sh` and the pins embedded in `Install.command`); check them, Team ID included, before
  opening it.

### DMG path

Needs Apple silicon and macOS 26 or later (`unsupported_platform` otherwise).

1. Download the DMG. Checks before opening it: `xcrun stapler validate <dmg>`;
   `spctl -a -vv -t open --context context:primary-signature <dmg>`, which should print `accepted`
   and `source=Notarized Developer ID`; and `codesign -dv <dmg> 2>&1 | grep TeamIdentifier`, which must
   print `TeamIdentifier=256X5MUA6S`, because spctl accepts any notarized team. The release build runs
   all three (the last as the Team ID's designated requirement) before it accepts the image. A
   `shasum -a 256` match against the `SHA256SUMS` asset only shows the DMG is the uploaded one.
2. Open the DMG and double-click `Install.command`. It runs in Terminal. **Known limit:**
   `Install.command` is not code-signed, so no notarization ticket names it; the DMG's signature and
   ticket cover the image it ships in. Whether Gatekeeper runs a double-clicked unsigned script from a
   stapled, quarantined image, or asks for System Settings > Privacy & Security > Open Anyway, has not
   been observed, because no signed image exists; the owner-session run records it
   ([owner checklist](plans/0.53-signing-owner-checklist.md), section 7). The curl path below does not
   depend on it.
3. `Install.command` checks that it was started by an absolute path and that `bootstrap.sh`,
   `bootstrap-verifier`, `release.tar.gz` and `release-index.json` sit beside it as regular files
   (not links). It then runs `/usr/bin/env -i /bin/sh -p bootstrap.sh` with its embedded pins:
   `--seed-sha256`, `--seed-bytes`, `--trusted-index-sha256` and, for a Developer ID kit,
   `--seed-team <TEAMID>`. Nothing is read from the kit's `release-pins.json` at install time.
4. `bootstrap.sh` copies the seed to a private stage under `/private/tmp`, checks its sha256 and size,
   with `--seed-team` checks its Developer ID signature, and runs it. The seed authenticates the index
   and every archive member before extraction, and the installer from the extracted tree then does the
   [bundle installation](#private-bundle-0530dev0) and client registrations above. The stage directory
   is not removed by the scripts.
5. `Install.command` prints the installer's JSON result and accepts exactly the results the installer
   returns for a kit. After a first install it continues only when `status` is `activation_required`
   with `installation` `installed` (or `reused` on a repeat) and the activation argv is exactly the
   installed release's `python3 -I -S -B launcher.py connect`; it then prints `Installed. Starting the
   Telegram login (connect) in this window.` After an [upgrade](#upgrade) or its repeat (`status:
   upgraded` with `installation` `upgraded` or `reused`), or a repeat of the release a finished
   [rollback](#rollback) went back to (`status: rolled_back` with `installation: reused`), whose
   `release_root` is this kit's release, it prints `Installation is up to date. Running connect in this
   window; a ready session reports already_connected.` Either way it runs that release's `connect` in
   the same Terminal, so the QR page opens and the final result prints there; scan it as under
   [Connect with a QR code](#connect-with-a-qr-code-primary-path). With a ready session `connect`
   reports `already_connected` and asks for nothing. No assistant and no separate `wait` are needed.
   `Install.command finished.` follows a successful `connect`.
6. Restart Claude Desktop if the result says so (below), reconnect the other clients, and have an
   assistant call `_manifest` with `check_broker=true` and make one bounded own-chat read or search.

`Install.command --prepare-only` authenticates and extracts without installing and prints the
verification result. It accepts no other argument.

`Install.command` passes no `--claude-code`: a first install uses `user`, and an upgrade keeps the
installed mode. To choose another mode for a first install, or to pass `--adopt-legacy`, run
`bootstrap.sh` directly as in the next section.

Its refusals print a text line and `{"schema_version":1,"error_code":"<code>"}` on stderr:

| Code | Exit | Meaning |
| --- | --- | --- |
| `usage` | 2 | An argument other than `--prepare-only`. |
| `kit_unavailable` | 3 | Not started by an absolute path, or the folder cannot be resolved. |
| `kit_incomplete` | 3 | A required kit file is missing or is a link. |
| `bootstrap_failed` | 4 | `bootstrap.sh` or the installer refused; its own line is printed first. A `bootstrap.sh` refusal is `{"schema_version":1,"error_code":…,"retained_stage":…}`; an installer refusal is `{"status":"refused","code":…}`, with `pending_action` for `transaction_pending` ([codes](reference.md#installer-refusal-codes)). |
| `activation_unexpected` | 5 | The installer result was neither a first install (`activation_required` with `installation` `installed` or `reused` and the exact activation argv) nor an upgrade or repeat of this kit's release (`upgraded` with `installation` `upgraded` or `reused`, or `rolled_back` with `installation: reused`). |
| `activation_failed` | 6 | `connect` ended unsuccessfully; its own result is printed above. |

`bootstrap.sh` refuses with `{"schema_version":1,"error_code":…,"retained_stage":…}` on stderr:
`selection_invalid` (an unknown, repeated or malformed option, a bad pin, `--claude-code` outside
`user|plugin|skip`, or `--adopt-legacy` that is not the last token), `unsupported_platform`,
`dependency_unavailable` (a stock tool is missing), `integrity_failure` (a pin, size, owner, mode,
link count or ACL check failed), `seed_signature_invalid`, `bootstrap_verification_failed` (the seed
refused the index or archive) and `interrupted`. The installer's own refusals are listed in the
[reference](reference.md#installer-actions-and-result-fields).

### Curl path

Without the DMG, take the pins from the trust root (the tagged repository copy of `release-pins.json`,
[above](#publisher-team-id-and-what-binds-what)), fetch the four assets into a fresh folder you own,
and check every one of them, and the Team ID, before running anything. The plugin's installer fetches
the same four assets from the same URL pattern and checks them against its own copy of the pins. No
release exists yet, so these URLs do not resolve. Replace `<version>` with the release you install (for
example `0.53.0`); `KIT` must be a new absolute folder you own, and `tg_check` compares one file with the
size and sha256 its pin names.

```bash
VERSION=<version>
KIT="$HOME/tgmcp-kit"
REPO=https://github.com/flydge/unofficial-telegram-mcp
PINS=https://raw.githubusercontent.com/flydge/unofficial-telegram-mcp/v$VERSION/plugins/telegram-local/release-pins.json
tg_ready=no
/bin/mkdir -m 700 "$KIT" && /usr/bin/curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 --fail \
  --silent --location --output "$KIT/release-pins.json" "$PINS" && tg_ready=yes
for name in bootstrap.sh bootstrap-verifier release-index.json release.tar.gz; do
  [ "$tg_ready" = yes ] && /usr/bin/curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 --fail \
    --silent --location --output "$KIT/$name" "$REPO/releases/download/v$VERSION/$name" || tg_ready=no
done
tg_pin() { /usr/bin/plutil -extract "$1" raw -o - "$KIT/release-pins.json"; }
tg_check() {
  [ "$(/usr/bin/stat -f %z "$KIT/$2")" = "$(tg_pin "${1}_bytes")" ] &&
  [ "$(/usr/bin/shasum -a 256 "$KIT/$2" | /usr/bin/cut -d ' ' -f 1)" = "$(tg_pin "${1}_sha256")" ]
}
if [ "$tg_ready" = yes ] && [ "$(tg_pin team_id)" = 256X5MUA6S ] && [ "$(tg_pin publication)" = published ] &&
   [ "$(tg_pin version)" = "$VERSION" ] && tg_check bootstrap bootstrap.sh &&
   tg_check seed bootstrap-verifier && tg_check index release-index.json && tg_check archive release.tar.gz
then
  /usr/bin/env -i /bin/sh -p "$KIT/bootstrap.sh" \
    --seed "$KIT/bootstrap-verifier" --seed-sha256 "$(tg_pin seed_sha256)" --seed-bytes "$(tg_pin seed_bytes)" \
    --archive "$KIT/release.tar.gz" --index "$KIT/release-index.json" \
    --trusted-index-sha256 "$(tg_pin index_sha256)" --seed-team "$(tg_pin team_id)" --claude-code user
else
  echo 'Stopped: the pins, the Team ID or a file does not match. Run nothing from this folder.'
fi
```

Paste it into zsh, bash or sh as it is: it has no comments, which an interactive zsh would run as
commands, and the quotes around `=https` keep zsh from expanding it. Nothing runs before every check
passes: the folder is new and every download succeeded, the Team ID is the publisher's `256X5MUA6S`,
the pins say `published` and name this version, and `bootstrap.sh`, the seed, the index and the archive
each match their pinned size and sha256. Never take the pins from the `release-pins.json` uploaded
next to the assets: it would come from the same place as the files it is meant to check. Each input
must be a regular file you own, with one link and no group or world write bit; the seed is at most
2 MiB and the archive at most 1 GiB. For an ad hoc dry-run kit on the build Mac (`team_id` empty,
`unreleased`), run `bootstrap.sh` from that kit with its own pins and without `--seed-team`; it is a
test artifact.

The result is the installer's JSON with `activation_command`. Run that command from native Terminal
(`connect` then runs inline), or let an assistant run it and `wait` as under
[Connect with a QR code](#connect-with-a-qr-code-primary-path). Options `--installation-root`,
`--codex-home` and `--skills-root` (absolute paths) override the default locations.

### Claude Code: user scope, plugin or skip

`bootstrap.sh` takes `--claude-code user|plugin|skip` and passes it on only when it is given. The mode
is recorded in the installation. Without the option a first install uses `user`, and an upgrade, a
repeat or a resumed install keeps the recorded mode; an explicitly different mode refuses
(`upgrade_conflict` on an upgrade).

| Mode | Claude Code state | Result fields |
| --- | --- | --- |
| `user` | The user-scope `telegram_search` entry and the `~/.claude/skills` copies described above. | `claude_code`: `registered`, `reused`, `failed`, `unavailable` or `not_found`; `claude_skills`: `installed` or `skipped`. |
| `plugin` | No user-scope entry and no `~/.claude/skills` copies: the plugin supplies the server and the skills. | `claude_code: plugin`, `claude_skills: skipped`. If a user-scope entry exists, `claude_code_user_entry: present`; the installer never removes it. |
| `skip` | Claude Code is not touched. | `claude_code: skipped`, `claude_skills: skipped`. |

User scope and the plugin are exclusive because both at once would list the tool set twice. Codex, the
`~/.agents/skills` copies and Claude Desktop are registered in every mode.

### Claude Desktop

When `~/Library/Application Support/Claude` exists, the installer adds one
`mcpServers.telegram_search` entry to `claude_desktop_config.json` there. Its `command` is the installed
release's `python3` and its `args` are `-I -S -B <release>/launcher.py mcp`, the same runtime the other
clients use.

- With no `Claude` directory the result is `claude_desktop: skipped` and nothing is read or written.
- The file is read as strict JSON, at most 1 MiB, without following links. It must be yours, have one
  link and no group or world write bit; Claude Desktop's own 0644 is accepted. Anything else reads
  as `unavailable`.
- An absent entry is added once, in Claude Desktop's own layout (two-space indent). Every neighbouring
  server and value keeps its meaning, and every number keeps its exact spelling; a file for which that
  cannot be guaranteed refuses the install as `claude_desktop_config_unsupported` before anything is
  created. The file keeps its mode; a file the installer creates is `0600`.
- An identical entry is `reused`. A different `telegram_search` entry (for example the 0.51 manual one)
  refuses the whole install as `claude_desktop_registration_conflict` before anything is created.
  Remove it yourself, or see [--adopt-legacy](#adopting-entries-the-installer-does-not-own---adopt-legacy).
- After `claude_desktop: registered`, the result carries `claude_desktop_next_action:
  restart_claude_desktop`. Quit Claude Desktop and open it again; it reads its configuration only at
  launch. For `failed` or `unavailable` the result carries `claude_desktop_manual_entry`, a nonsecret
  JSON object to merge into the file yourself.
- Claude Desktop may rewrite its configuration from memory while it runs. The installed launcher's
  `status` action reports `clients.claude_desktop` as `ok`, `drift` (with `claude_desktop_manual_entry`),
  `not_registered`, `skipped` or `unavailable`, reading the file only.

### Claude Code plugin

The repository root is a Claude Code marketplace (`.claude-plugin/marketplace.json`, marketplace
`unofficial-telegram-mcp`) holding one plugin, `telegram-local`. Both names, and the plugin's MCP server
name `tg`, are placeholders until the product is renamed. The plugin carries the `telegram-search` and
`telegram-setup` skills (byte-identical copies of `skills/`) and an MCP server whose tools are named
`mcp__plugin_telegram-local_tg__<tool>`; every such name fits the 64-character limit.

1. In Claude Code, add the marketplace and install the plugin:
   `/plugin marketplace add flydge/unofficial-telegram-mcp`, then
   `/plugin install telegram-local@unofficial-telegram-mcp`. The repository is not published yet, and
   this guide does not verify Claude Code's own command syntax. The plugin's files, `bin/install`,
   `bin/mcp-entry` and its `release-pins.json` included, are whatever commit of the repository's branch
   your marketplace clone holds: the marketplace names the plugin by a relative path with no ref, and a
   marketplace update can change all of them together. Nothing pins a plugin commit, so that branch is
   the trust root of this path. Claude Code runs `bin/mcp-entry` as soon as the plugin is enabled, and
   `bin/install` runs in step 3; both are shell scripts that run as you, and no signature, Team ID or pin
   covers them. Read both (each is a short POSIX `sh` script) and compare the plugin with the release tag
   (step 3) before you install or enable it; where Claude Code keeps the marketplace clone before that
   is Claude Code's behaviour and is not verified here.
2. Until the runtime is installed, the plugin's server `tg` stops at once with
   `{"schema_version":1,"error_code":"runtime_not_installed"}` on stderr.
3. Before installing the runtime, inspect `<plugin root>/release-pins.json`, where `<plugin root>` is
   the directory the plugin is installed in (Claude Code's `${CLAUDE_PLUGIN_ROOT}`): its `team_id` must
   be the publisher Team ID `256X5MUA6S` and its `version` the release you expect, and it should equal
   `plugins/telegram-local/release-pins.json` at the tag `v<version>` in the repository
   ([trust root](#publisher-team-id-and-what-binds-what)), which this prints `same` for:

   ```bash
   /usr/bin/curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --location \
     "https://raw.githubusercontent.com/flydge/unofficial-telegram-mcp/v<version>/plugins/telegram-local/release-pins.json" |
     /usr/bin/cmp - "<plugin root>/release-pins.json" && echo same
   ```

   The pins match is not enough: a branch with changed scripts and the right pins passes it. Compare every
   plugin file with the tag the same way, which prints one line per file and `same` for each match:

   ```bash
   for f in .mcp.json .claude-plugin/plugin.json bin/install bin/mcp-entry release-pins.json \
       skills/telegram-search/SKILL.md skills/telegram-setup/SKILL.md; do
     /usr/bin/curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --location \
       "https://raw.githubusercontent.com/flydge/unofficial-telegram-mcp/v<version>/plugins/telegram-local/$f" |
       /usr/bin/cmp -s - "<plugin root>/$f" && echo "same $f" || echo "DIFFERENT $f"
   done
   ```

   Only `same` lines show that the clone is at the tag. That still trusts the repository: whoever can push
   to it can move the tag too. The Team ID check below binds the downloaded seed, not these scripts.

   Then install the runtime with `/bin/sh -p "<plugin root>/bin/install"` and no arguments.
   The script refuses `plugin_invalid` unless it is started by an absolute path ending in
   `/bin/install`. It reads the plugin's committed `release-pins.json` and downloads the four assets
   `bootstrap.sh`, `bootstrap-verifier`, `release-index.json` and `release.tar.gz` from the fixed
   GitHub Releases URL above, once each with `/usr/bin/curl --proto =https --fail` and no retry, into a
   private `~/.telegram-local-install.<random>` folder. Each must match its pinned size and sha256
   before anything runs. Then the pinned `bootstrap.sh` installs with `--claude-code plugin` and
   `--seed-team <the pinned Team ID>`. The folder is removed on success and kept on failure, and a
   refusal names it as `retained`.
4. The script prints the installer JSON on stdout and exits 0 for exactly the results the installer
   returns for a kit: a first install (`status: activation_required` with `installation` `installed`, or
   `reused` on a repeat), an [upgrade](#upgrade) or its repeat (`status: upgraded` with `installation`
   `upgraded` or `reused`), or a repeat of the release a finished [rollback](#rollback) went back to
   (`status: rolled_back` with `installation: reused`); any other result refuses as `result_invalid`.
   After a first install run its `activation_command` (`connect`) as for any install; the paired
   `telegram-setup` skill walks an assistant through it. Reconnect with `/mcp` (restart Claude Code if
   `/mcp` does not list the server).
5. If `claude_code_user_entry` is `present`, remove the older user-scope entry yourself with
   `claude mcp remove telegram_search -s user`, or the tool set appears twice.

`bin/install` refuses with `release_not_published` (unreleased pins), `pins_invalid`,
`plugin_invalid`, `download_failed`, `pin_mismatch`, `bootstrap_failed`, `result_invalid`,
`dependency_unavailable`, `home_invalid`, `workspace_failed`, `cleanup_failed`, `invalid_arguments` or
`interrupted`, each as one JSON line on stderr. Published pins must carry a Team ID.

How a Claude Code session exposes `${CLAUDE_PLUGIN_ROOT}` to a shell command is Claude Code's
behaviour and is not verified here; if the directory is unknown, ask the user to run the command.

### Upgrade

Run the newer kit's `Install.command`, or the same `bootstrap.sh` command with the newer kit's pins and
either no `--claude-code` or the installed mode. `Install.command` keeps the installed mode and then
runs `connect` in the same window (see [DMG path](#dmg-path)). A newer plugin's `bin/install` upgrades
a `plugin`-mode installation: it passes `--claude-code plugin`, prints the `status: upgraded` result (or
the repeat's) and exits 0; over a `user` or `skip` installation it refuses as `bootstrap_failed`, after
the installer's own refusal line.
Over a receipt-bound installation of another release, `bootstrap-install` runs an upgrade transaction
instead of a first install. Refusals for a downgrade or a foreign or changed object come before the
transaction exists, so they change nothing; for a pending send see the gate below, and for a refusal
after the broker stop [its own section](#a-refusal-after-the-broker-stop). The steps, each journaled so
that an interruption resumes by repeating the command:

1. The new release tree is copied next to the current one; older release trees stay in `releases/`.
2. The broker is observed and the pending-send gate runs (below). Right after it the exact receipt-bound
   broker is booted out, if it is loaded, and its LaunchAgent plist is rewritten: all before any client
   file is written.
3. The Codex subtree, the skill copies, the Claude Code user-scope entry (in `user` mode) and the
   Claude Desktop entry are replaced. Only entries the installation owns are replaced (see
   [owned client entries](#owned-client-entries)); an entry you removed is added back, and a different
   entry refuses (`registration_conflict`, `claude_registration_conflict`,
   `claude_desktop_registration_conflict`). Each client file is written over its bytes as they are at
   that moment: an edit you make while the upgrade runs (another server, a profile, a setting) is kept
   byte for byte, and our entry removed meanwhile is added back. Only our own Codex table or skill copy
   changed since the upgrade started (or right after the upgrade wrote it) stops it there
   (`target_conflict`, `skill_conflict`; see [a refusal after the broker stop](#a-refusal-after-the-broker-stop));
   a changed Claude Code or Claude Desktop entry is left alone and reported `failed`.
4. Only the `expected_package_version` line of `runtime.toml` is rewritten, to the new version; every
   other byte, capabilities included, stays. A file that cannot be changed that way refuses as
   `policy_conflict`.
5. `receipt.json` is rewritten and `current.json` is switched last. The broker is bootstrapped again
   only if it was running before.

The result is `status: upgraded` with `previous_release_root`, `service` and `next_action:
restart_mcp_clients`. Reconnect every client: a running proxy that loaded the old policy lists only
`_manifest` until it restarts. The Telegram session, the enrollment, the managed key and the Keychain
are not written.

**Pending-send gate.** If the broker is running, the installer asks it, through its own release's
client (a 0.51 or 0.52 source-venv broker adopted with `--adopt-legacy` included), whether any client
context is connected. A context may hold a draft or an unresolved send outcome, and an unknown answer
counts as pending. It asks before the transaction exists, again once the new release is copied, and a
last time right before the broker is stopped, which comes before any client file is written. Any such
answer refuses as `pending_send_unresolved` with every client entry, the broker, its plist and the
session unchanged. A refusal at the first check changes nothing at all; at a later one the new release
tree is already copied and the upgrade stays unfinished, so `rollback` and another kit refuse as
`transaction_pending` (`pending_action: upgrade`) until the same command is repeated (or `uninstall`,
which goes over the unfinished upgrade, removes the installation). Quit the
MCP clients, wait for their contexts to idle out (300 seconds by default), and repeat. An uncertain send
is never retried by an upgrade. From the broker stop until the upgrade finishes, or until a refusal
restores the broker (below), running MCP clients cannot reach the broker.

#### A refusal after the broker stop

A refusal raised after the broker stop and before `current.json` switches first restores the service,
then refuses. Examples: `target_conflict` when you changed the `telegram_search` table in
`~/.codex/config.toml` while the release was copied, `skill_conflict` for an edited skill copy,
`policy_conflict` for a changed `expected_package_version` line, `metadata_conflict` when `receipt.json`
changed. The restore puts the `expected_package_version` line of `runtime.toml` back if this upgrade
already rewrote it (to the value recorded before the rewrite), writes the previous LaunchAgent plist back
byte for byte (its bytes are kept in the transaction before the rewrite) and bootstraps the previous
broker again if it was running, so the installed release keeps serving. A late refusal leaves exactly
this:

- `current.json` and `receipt.json` still name the installed release; its broker, its plist and the
  `runtime.toml` version line are as they were before the upgrade. Only a refusal at the very last step,
  the switch of `current.json` itself (a `current.json` changed by hand, or a disk error), comes after
  `receipt.json` already names the new release: then neither release starts until the repeat or an
  uninstall finishes.
- The new release tree and the unfinished transaction stay.
- Every client entry and skill copy written before the refusal stays: in step order the Codex subtree,
  the skill copies, the Claude Code entry and the Claude Desktop entry (a Codex refusal comes before any
  of them). The entries already name the new release, so those clients cannot start the server until the
  upgrade commits: the launcher starts only the release the receipt names.
- `rollback` and another kit refuse as `transaction_pending` (`pending_action: upgrade`); `uninstall`
  does not (below).

The upgrade is now parked, and two commands end it; `rollback` keeps refusing until one of them has run:

- **The repeat.** Put back or remove the object the code names (a removed `telegram_search` table or
  skill copy is written again, also one the upgrade had already written), then repeat the same command:
  it passes the pending-send gate, stops the broker again and resumes. There is no limit on how often an
  upgrade may be refused and repeated. Every run before `current.json` switches makes the broker the new
  release's whatever an earlier run left: an old broker started again meanwhile (a login loads the plist
  on disk) is gated and stopped again, and the plist is rewritten until it is the new release's. After the
  switch only the new release's plist is bootstrapped. So a repeat killed anywhere, its broker stop
  included, still ends with `current.json`, the `runtime.toml` version line, the plist and the running
  broker on the same release once it is repeated again.
- **`uninstall`.** It goes over the parked upgrade: it removes what the installed release owns and
  everything the upgrade already wrote (the Codex entry, skill copies, and Claude Code and Claude Desktop
  entries that name the new release), each only while it is exactly what was written, then the broker,
  its plist and the installation root with both release trees. Adoption backups are kept as usual, and an
  entry or skill copy you changed is kept (`kept`).

The same holds for a rollback (`pending_action: rollback`). For an adopting first install
(`pending_action: install`, repeated with `--adopt-legacy`) the foreign plist and broker come back; there
the repeat is the way out, since there is no installed release for `uninstall` to remove yet (it refuses
as `transaction_pending`). A restore never overwrites a plist changed since the stop: then the refusal is
`service_conflict` (or `service_restart_failed` when the bootstrap fails) and the broker stays stopped
until a repeat succeeds. A repeat after a kill during the restore finishes the restore and refuses with
the same code; until that repeat has run, `uninstall` also refuses as `transaction_pending`.

**Other refusals.** A new package version lower than the installed one refuses as `downgrade_refused`
(going back is only `rollback`). A different Codex home, skills root or home than the installed receipt,
or an explicitly given `--claude-code` mode other than the installed one, refuses as `upgrade_conflict`,
also when the kit is the installed release's own (a repeat).
Another install, upgrade, rollback or uninstall started while one is unfinished refuses as
`transaction_pending`, and the refusal's `pending_action` (`install`, `upgrade`, `rollback` or
`uninstall`) names the command to repeat first. The one exception is an `uninstall` over a parked upgrade
or rollback (above).

### Rollback

`rollback` returns to the release before the last upgrade. Run it with the current release's launcher;
`release_root` in `current.json` names it:

```bash
ROOT="$HOME/Library/Application Support/TelegramSearch"
RELEASE=$(/usr/bin/plutil -extract release_root raw -o - "$ROOT/current.json")
/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C \
  "$RELEASE/python/bin/python3" -I -S -B "$RELEASE/launcher.py" rollback
```

It re-registers the previous release's entries by the same owned-entry rules, rewrites the broker plist,
passes the same pending-send gate, restores the `runtime.toml` version line and switches `current.json`
back. Client edits made meanwhile are kept as for an upgrade, and a refusal after its broker stop restores
the current release's broker first ([a refusal after the broker stop](#a-refusal-after-the-broker-stop)).
The restored receipt keeps the previous release's schema but records the rollback's own
`claude_code` and `claude_desktop` outcomes, and the entries the rollback wrote are owned like an
upgrade's, so a later upgrade replaces them and `uninstall` removes them. The result is `status: rolled_back` with `next_action: restart_mcp_clients`. Repeating the
rollback, or running the kit of the release it went back to again (its `Install.command`, the same
`bootstrap.sh` command, or for a `plugin`-mode installation that release's plugin `bin/install`), changes
nothing and returns that result with `installation: reused` and without `service`; the newer kit
upgrades again.

It refuses as `rollback_unavailable` when there is no previous release, and as `rollback_refused` when
newer state exists: `enrollment.json` differs from its state when the upgrade began (for example after
a new `connect` enrollment), `runtime.toml` differs from its earlier bytes in more than the version line,
or the previous release's manifest or Codex ownership no longer matches. A rollback does not undo a
send, move or edit that already happened. Going back to a 0.51 install is a manual procedure; see
[rollback](security.md#rollback).

### Owned client entries

The installation owns a Claude Code or Claude Desktop entry that its own first install, upgrade or
rollback wrote (that step's result was `registered`), and only while the entry is still exactly what it
wrote. When a later upgrade or rollback ends otherwise for that client (`failed`, `not_found`,
`unavailable`, or `reused` for an entry it did not write), it replaced nothing, so the entry it left in
place stays owned: the next upgrade replaces it and `uninstall` removes it. An entry that was already
there (`reused` on a first install), one you wrote, and one you changed are never owned. Ownership is
read from the installation's own transaction journals, the same way for every receipt schema.

### Uninstall

`uninstall` removes only objects the receipt owns. Run it the same way as `rollback`, with `uninstall`
in place of `rollback`. In order it removes: the broker plist (the exact LaunchAgent label is booted out
first, after the pending-send gate); the Codex subtree, only while it is exactly what the receipt
recorded; the Claude Code user-scope entry (through the `claude` CLI, `user` mode only); the Claude
Desktop entry, leaving its neighbours and the file's mode; the skill copies the receipt binds; and
finally the installation root with all releases.

**The installation root belongs to the installer.** `~/Library/Application Support/TelegramSearch`
holds only what the installer writes: `releases/`, `transactions/`, `receipt.json`, `current.json`,
`adopted.json` and `.install.lock`. Do not keep your own files or folders there. An install, upgrade or
rollback refuses a root with any other top-level name (`runtime_conflict`), but `uninstall` removes the
root as one owned folder, with everything inside it, including anything you added (a file at the top
or a copy under `releases/`). Only the `--adopt-legacy` backups are moved out first
(`legacy_backups`). The Telegram session and policy are not in this folder.

Each part reports `removed`, `absent`, `kept` (not an entry the installation owns, or changed since it
was written, so left alone), or for a client `skipped`, `unavailable` or `not_found`, in the result fields `service`, `codex`, `claude_code`,
`claude_desktop` and `skills`. `uninstall_incomplete` means an entry the installer owns was still
present after its removal attempt; repeat the command. An unfinished first install refuses as
`transaction_pending` with `pending_action: install` and is left as it is; repeat the install first. An
upgrade or rollback parked by a refusal (before its broker stop, or after a late refusal's restore; see
[a refusal after the broker stop](#a-refusal-after-the-broker-stop)) does not block it: the entries and
skill copies that change already wrote are removed as the installation's own. A change interrupted with
its broker stopped, or whose restore has not finished, refuses as `transaction_pending`; repeat it first.
That check, and the read of the install intent the last step needs, come before anything is removed:
an unreadable intent refuses as `initialization_conflict` with nothing changed, and only the exact
staged folder the intent records counts as an unfinished first install, so something else that later
appeared under that folder's name stays and does not stop the uninstall.

Last, the root is renamed to `~/Library/Application Support/.TelegramSearch-removed-<random>`; that
folder is removed, then `.TelegramSearch.install-intent.json`. Only the folder the intent binds (the
root's own directory) is removed; a lookalike folder with a similar name stays. The rename is the
uninstall's last journaled step: every entry and skill copy it removes is gone by then. If uninstall is
interrupted after the rename, even partway through removing the folder (whose own launcher may then no
longer start), the next install first finishes removing that folder and the intent, then installs.
`uninstall` repeated with `<that folder>/releases/<sha256>/launcher.py`, while that launcher still
starts, finishes the same way and reports `installation: absent`. If the interruption came after the
folder was gone, the next install removes the leftover intent itself.

It never touches the Telegram session, `enrollment.json`, `runtime.toml`, the managed key or any
Keychain item. These stay under `~/Library/Application Support/TelegramSearchMCP`. The Claude Code
plugin itself is not removed here; remove it with Claude Code. After uninstall, clients still hold the
old tool list until they reconnect.

### Adopting entries the installer does not own: --adopt-legacy

By default the installer refuses foreign entries: a different `telegram_search` entry in Codex, Claude Code
user scope or Claude Desktop, and a broker plist it did not write (for example a source-venv broker). To
migrate a 0.51 or 0.52 installation, pass `--adopt-legacy` as the **last** token of the `bootstrap.sh`
command (any other position is `selection_invalid`). `Install.command` and the plugin never pass it. For
the owner's migration, with `KIT` the kit folder and the pins from the trust root (the tagged repository
copy of `release-pins.json`, or for a kit from a checked DMG its own copy), all four files checked as in
the [curl path](#curl-path):

```bash
/usr/bin/env -i /bin/sh -p "$KIT/bootstrap.sh" --seed "$KIT/bootstrap-verifier" \
  --seed-sha256 <seed_sha256> --seed-bytes <seed_bytes> --archive "$KIT/release.tar.gz" \
  --index "$KIT/release-index.json" --trusted-index-sha256 <index_sha256> --seed-team <team_id> --adopt-legacy
```

then the printed activation command. It applies to a first install and to an upgrade, not to `rollback`.

What it checks and what it does not: the recipe itself adds no check. It runs `bootstrap.sh` from the
kit folder as given, so the release is checked only as far as you first checked the four files and the
Team ID against the pins from the trust root (as in the curl recipe); `bootstrap.sh` then checks the
seed's sha256, size and Developer ID requirement for that Team ID, and the seed the index and every
archive member. Adoption does not check that what it replaces belongs to a 0.51 or 0.52 install:
whatever `telegram_search` entry and broker plist it finds is backed up and replaced. The pending-send
gate runs the interpreter that the old broker's plist names, so adopting trusts that interpreter. The
backups are kept, never restored automatically.

- Before anything is created, each foreign entry's exact bytes (at most 1 MiB each) are saved in the
  transaction and listed in `adopted.json` in the installation root and in the receipt. The result
  lists the adopted clients in `adopted`.
- A foreign broker is stopped only after the pending-send gate, and its plist is replaced by the new
  definition. The `expected_package_version` line of `runtime.toml` follows, whatever its old value. A
  refusal after that stop and before the install commits writes the foreign plist back byte for byte and
  starts that broker again if it was running; the install stays unfinished (`transaction_pending`,
  `pending_action: install`) until the same command, with `--adopt-legacy`, is repeated
  ([a refusal after the broker stop](#a-refusal-after-the-broker-stop)).
- A Codex config edited while the install runs is merged; only a change of the adopted
  `telegram_search` table itself refuses (`foreign_registration`).
- `uninstall` keeps every backup, moved beside the removed root as
  `~/Library/Application Support/.TelegramSearch-adopted-<random>`; the result names it in
  `legacy_backups`.

### Quarantine, notarization tickets and the Keychain

- `bootstrap.sh` copies the seed with `cp`, which keeps `com.apple.quarantine` where the file has it (a
  browser download does), so that Gatekeeper can evaluate the notarized seed when it runs. A bare Mach-O
  cannot carry a stapled ticket, so its notarization ticket would be fetched online on that first run, and
  an offline first run of a quarantined seed could be refused. **Not yet observed:** no signed seed has
  run, and on the DMG path it also depends on whether files on the mounted image carry quarantine; the
  owner-session run records it. The DMG itself is stapled.
- A file fetched with `curl` has no quarantine attribute, so Gatekeeper does not evaluate the seed. For
  that path, and for the plugin, the sha256 pins from the trust root and the Team ID check bind the seed.
- The seed writes the extracted files itself and copies no extended attributes, so they carry no
  quarantine. The archive `release.tar.gz` is not a notarization subject; see
  [the notarization scope](security.md#signing-and-team-id-trust-chain-053-candidate).
- The database key's Keychain item is tied to the code identity of the runtime's `python3`. A runtime
  signed with a different identity than the one that created the key (for example the owner's earlier
  source install, or a switch between the ad hoc and the Developer ID build) may make macOS ask once
  whether to allow access. This has not been observed with a signed build; that run is the owner's
  migration test. The interpreter's signing identifier is the same in every release, so that its
  designated requirement does not change between upgrades; that is the design and is not yet
  observed on a signed build.

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

The check prints `INITIALIZING_TDLIB` followed by `AUTHORIZATION_READY` to stderr and exits `0` on success; a Homebrew/source install also prints `TDLIB_OPENSSL_KEG: openssl@3 <version>` after the first line. It initializes the session when needed but does not search or read messages. Exit `2` means authorization is blocked; exit `1` indicates another setup, transport or compatibility failure.

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

This pin is for 0.51.0 only; a policy written by the 0.52 `connect` pins `0.52.0`.

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
| Move one chat between Main and Archive (candidate) | `send`, `read` and `archive_chats`; see the [archive and edit reference](reference.md#archiveunarchive-and-own-text-edit-f17a). Off by default; each move still needs the preview, approval and local confirmation. Requires `read` because `prepare_chat_archive` reveals that chat's title, kind and list membership |
| Edit the plain text of one own text message (candidate) | `send`, `read` and `edit_own_text`; see the [archive and edit reference](reference.md#archiveunarchive-and-own-text-edit-f17a). Off by default, independent of `archive_chats`; each edit still needs the preview, approval and local confirmation. Requires `read` because `prepare_text_edit` reveals that message's text, formatting and edit date |

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
