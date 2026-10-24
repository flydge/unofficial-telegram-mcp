# Changelog

## 0.56.0rc1 — release candidate, 2026-10-24 (not published)

Release candidate of 0.56.0, ordered albums ([release notes](docs/release-notes-0.56.0rc1.md), [spec](docs/plans/0.56-albums-spec.md),
[task record](docs/plans/0.56-albums-spec.tasks.md)): `prepare_album_send`, `send_prepared_album` and
`get_album_send_status` (58 tools) behind the default-off `album_send` capability and its `albums` profile,
with `video_send` also required for a video item; 2–10 photos and videos or documents (each with an optional
`display_name`), one owner dialog that lists every item, exactly one `sendMessageAlbum` attempt, per-item
outcomes with `partial`, never a resend.
Technical evidence only; the owner session is pending.

## 0.55.0rc1 — release candidate, 2026-10-24 (not published)

Release candidate of 0.55.0, multiple accounts ([release notes](docs/release-notes-0.55.0rc1.md),
[spec](docs/plans/0.55-multiple-accounts-spec.md), [task record](docs/plans/0.55-multiple-accounts-spec.tasks.md)). Technical evidence only: unit tests, synthetic homes,
fake launchd, client CLIs and TDLib sessions; two really authorized accounts are an owner-session row.

- Paths and registry: one `AccountPaths` per account, the default byte-identical to 0.54 (paths, labels,
  plists, client entries, receipts, `runtime.toml` and the schema fingerprint, checked against
  v0.54.0rc1); a closed owner-only alias registry (`TelegramSearchMCP-accounts/registry.json`, at most
  eight aliases `[a-z][a-z0-9]{0,11}`) with the socket-path and tool-name budgets.
- `--account <alias>` on the launcher, the proxy, the broker and the transfer helper; one broker
  LaunchAgent and one connect job per alias; an alias's server names it in `serverInfo.name` and one
  instructions line (which also names the transfer helper's `--account <alias>`), and its QR page
  names it.
- The account pin: each account's first authorized user, `account_mismatch` and `account_duplicate` at
  every login before the handoff and before every broker operation outside a closed local exempt set,
  fail-closed; the user id is never printed. Once a registry exists the approval dialog names the alias
  and a masked account, and one owner dialog at a time holds across every account's broker (the
  owner-only `TelegramSearchMCP-accounts/dialog.lock`; another account's send meanwhile answers
  `dialog_unavailable`; a dialog already open when the first `account-add` runs is covered only by its own
  broker, for at most its 120-second timeout); a chat that is the account's own reads "Saved Messages" in both
  modes.
- Re-login hardening inside one broker: discovery `scan_` continuations bound to the account and
  generation, owner-aware draft routing.
- Installer: `account-add` and `account-remove` (journaled; alias entries for Codex with
  `required = false`, Claude Code user scope also in plugin mode, Claude Desktop beside the default;
  `accounts.json`; removal moves the session aside as `removed-<alias>-<32 hex>`, never deleting it);
  `status` over every account; `capabilities --account`; upgrade, rollback and uninstall over the
  default and every alias, with a pending-send gate per broker (a leftover broker of a removed release
  through its live socket), `login_in_progress`, `accounts_present` for a rollback below 0.55, and the
  registry moved aside once no alias is left. Only `account-add` creates an alias root; after an
  uninstall it repairs an alias the registry kept, moving its version line onto the current release only
  from an equal or older one. A rollback that keeps an alias on its newer release sets its plist aside
  (`newer_state_kept`). `account-add` reports a default broker that does not answer its one check as
  `default_check: unanswered` and goes on. New installer codes `accounts_present`,
  `accounts_inconsistent`, `account_remove_incomplete`, `release_not_current`, `login_in_progress` and
  `connect_lock_unsafe` (each with `account`), and `alias_broker_unreadable` (with `account` and
  `bootout_command`), plus the registry's codes.
- Alias logins and installer transactions never overlap: every transaction over aliases holds each
  alias's connect lock, and an alias login run from an installed release refuses `transaction_pending`
  (with `pending_action` and `pending_account`) while any transaction is unfinished, and
  `newer_state_kept` or `older_state_kept` (run `account-add <alias>`) while its `runtime.toml` names a
  newer or an older release; a login never creates an alias root (`alias_unavailable`).
- Isolation matrix: every handle kind one account issues is refused by another exactly like an unknown
  handle, on two real brokers over fake sessions.
- Docs: an Accounts section in the installation guide, the reference and the security notes, and the
  setup skill.

## 0.54.0rc1 — release candidate, 2026-10-23 (not published)

Release candidate of 0.54.0, conversational usability ([release notes](docs/release-notes-0.54.0rc1.md), [spec](docs/plans/0.54-conversational-usability-spec.md),
[task record](docs/plans/0.54-conversational-usability-spec.tasks.md)): short tool contracts with titles,
normalized annotations and prompts; argument refusals that name only schema parameters; approval statuses
`declined`, `timeout` and `dialog_unavailable` with one dialog renderer; `capability_disabled` reply results
and closed `next_action` hints; provider links in the newer readers; the `selection_ttl_seconds` policy
setting; owner capability profiles switched in Terminal with every MCP client quit (`capabilities show |
enable | disable`), which a later rollback keeps; one owner dialog at a time; a lean search skill with a
coverage test; a conversation harness and a consumer-matrix harness. Technical evidence only; owner-session
rows are listed in the task record.

## 0.53.0rc2 — release candidate, 2026-10-22 (not published)

Replaces 0.53.0rc1, whose own kit could not be installed by any path ([release notes](docs/release-notes-0.53.0rc2.md)).
Fixes from the independent re-review of rc1; unit, synthetic and real-broker test evidence only, and this
round's fixes are not re-reviewed yet. The owner-session rows of rc1 are unchanged.

- One version grammar at every site: the native seed refused `0.53.0rc1` (it admitted only `X.Y.Z` and
  `X.Y.Z.devN`). The seed, the plugin's `bin/install`, `release-pins.json`, the archive index, the
  installer's order, the launcher generator and the release gate now admit exactly `X.Y.Z`, optionally
  `aN`, `bN` or `rcN`, optionally `.devN`, with no leading zeros; tags are `v<version>` for a release or an
  `rcN` candidate. A test compiles the shipping seed and runs it against one shared table of admitted and
  refused versions, and `release_build.py` runs the kit's seed on the kit's index (`kit_failed`).
- No installer dead ends: a skill copy touched after the change's `receipt.json` is written no longer stops
  the repeat (and, after the commit, uninstall), for a first install too; after an upgrade's or rollback's
  commit a changed Codex table no longer does either; a broker plist removed by hand while a change is
  unfinished is written anew or restored from the transaction's copy; the previous plist bootstrapped again
  between the stop and the rewrite is stopped; repeated refusals in the Codex write window no longer reach
  `recovery_limit`.
- `uninstall` over a parked change removes a Claude Code or Claude Desktop entry the change wrote before an
  interruption cut off its outcome record.
- The pending-send gate asks the broker for sends in flight, approval dialogs open and unsettled send
  outcomes (new health field `pending_sends`), not only for client contexts.
- Fixed links to the renamed private-bundle heading; a test checks every Markdown heading link.

## 0.53.0rc1 — release candidate, 2026-10-22 (not published)

Release candidate of 0.53.0, client coverage and distribution ([spec](docs/plans/0.53-client-distribution-spec.md), [release notes](docs/release-notes-0.53.0rc1.md)).
Every item below has unit-test or ad hoc dry-run evidence only. The owner provisioned the Developer ID
identity and notary profile on 21.10.2026 (preflight ok). The signed and notarized build and its
Gatekeeper observation, the clean-user install, the owner's migration, the product name and publication
wait for the owner session; no signed DMG or public release asset exists yet.

- Product identity (phase A): every user-visible name comes from `product_identity.py`, with a
  rename-impact test and a name-length check that keeps every plugin tool name within 64 characters.
  TDLib now reports the product's device model and the package version. Internal compatibility names
  (the Python package, support paths, Keychain service, launchd labels, environment keys) are unchanged.
  The product name itself is still a placeholder (owner-pending).
- Reproducible payload (phase B): `packaging/prepare_native.py` reproduces the native lock from the pinned
  Homebrew kegs and `packaging/prepare_payload.py` builds the pruned payload from pinned inputs (the
  archive is about 81 MB, down from about 106 MB; two builds are identical). Manifest v2 (`publication`,
  `source_completeness`, `publisher`) and native lock v2 (a reviewed pin table with a Team ID check) are
  admitted next to v1. The Homebrew path runs the same TDLib version and commit probe as the bundle,
  and `--check` reports the OpenSSL keg.
- Signing and distribution (phase C):
  - `sign_payload.py` signs every Mach-O inside out with the hardened runtime and stable identifiers.
    The entitlement matrix gives `disable-library-validation` for the ad hoc identity; the Developer ID
    set is empty and stays unproven until the owner's signed run.
  - The seed is signed too, and `bootstrap.sh --seed-team <TEAMID>` checks its Developer ID requirement
    before running it (`seed_signature_invalid`).
  - `notarize_release.sh` notarizes the payload zip, builds the DMG, then signs, notarizes and staples it
    (`notarytool` is mocked in tests). `Install.command` runs `bootstrap.sh` with its embedded seed and
    index pins and then `connect` inline in Terminal, so one double-click covers install and login.
    `Install.command` is not code-signed (the DMG's signature covers it); how Gatekeeper treats the
    double-click is an owner-session observation.
  - `release_build.py` runs the whole chain to a corresponding-source archive, `signing_preflight.sh` and an
    [owner checklist](docs/plans/0.53-signing-owner-checklist.md) cover the identity and notary profile,
    and the release gate gains an optional bundle stage. The bundle is assembled without the application
    identity by default, or with one from a named Keychain service.
  - `release-pins.json` has one schema (`packaging/release_pins.py`): the release build writes it with
    the sha256 and size of `bootstrap.sh`, the seed, the index and the archive, `Install.command` is
    rendered from it, and the release commit copies it byte for byte into the plugin, checked by
    `release_build.py release-commit`. That committed copy at the release tag is the trust root of the
    curl and plugin paths, checked against the publisher Team ID `256X5MUA6S`; the curl recipe checks
    all four files before running anything. The native pin table approves that Team ID.
  - Script path guards refuse a newline, not only other control bytes.
  - Owner-session: the signed and notarized build and its Gatekeeper observation, and the
    application-identity decision. The Developer ID identity and notary profile were provisioned by the
    owner on 21.10.2026.
- Clients (phase D):
  - Claude Desktop: the installer adds one owned `mcpServers.telegram_search` entry to
    `claude_desktop_config.json` (bounded strict read, journaled compare-before-replace write, neighbours
    preserved or the install refuses). A different entry refuses the install
    (`claude_desktop_registration_conflict`), a missing Claude directory skips it, and the result asks
    for a restart.
  - `--claude-code user|plugin|skip` selects Claude Code's user scope, the plugin or nothing. The plugin
    `telegram-local` and a repository marketplace add byte-identical skills, a `/bin/sh -p` shim that reads
    `current.json`, and a `bin/install` that installs from pinned release assets. It refuses
    (`release_not_published`) while its pins are the unreleased placeholder. Nothing pins a plugin
    commit: the plugin is whatever commit the user's marketplace clone holds.
  - The server sends client-neutral `instructions` (1,378 characters). `_manifest` gains `available_tools`
    and `tool_capabilities`, and `tools/list` now shows only the tools the loaded policy enables (15 of 55
    under the default `read` and `artifacts` policy). The schema fingerprint still covers the full
    registration and is unchanged.
  - A structural STDIO recorder records a client's protocol revision, tool census and a hash of the tool list and,
    per call, the tool name, `isError`, the content blocks' types and byte sizes and whether structured
    content came back; never arguments or result content.
- Upgrade, rollback, uninstall and `--adopt-legacy` (phase E):
  - Re-running the installer with a newer kit upgrades in place: the new release is published next to the old
    one, owned registrations and skills are re-registered, the broker plist is rewritten, only the
    `expected_package_version` line of `runtime.toml` changes, and `current.json` switches last. A running
    broker with a connected client context refuses first (`pending_send_unresolved`), and a lower
    version refuses (`downgrade_refused`). A client file edited while a change runs is merged; only a
    change of our own entry refuses. A refusal after the broker stop first writes the previous plist back
    byte for byte and starts that broker again if it ran (stopping first a new broker a login started from
    the rewritten plist, and removing the temporaries killed client writes left), so the installed release
    keeps serving until the same command finishes the change (repeated as often as needed; every run before
    the switch makes the broker and its plist the new release's again, whatever a kill or a login left) or
    `uninstall` removes the installation together with what the change already wrote.
  - `launcher.py rollback` returns to the previous release while no newer state exists, and
    `launcher.py uninstall` removes only owned objects. The Telegram session, enrollment and Keychain are
    never written. The receipt moves to schema 2 and `status` reports Claude Desktop drift.
  - `--adopt-legacy`, as the last token of `bootstrap.sh`, replaces a foreign Codex, Claude Code or
    Claude Desktop entry or broker plist only after an exact backup is recorded. `Install.command` and
    the plugin never pass it.
  - `Install.command` and the plugin's `bin/install` accept an upgrade or a repeat as a success, and
    `Install.command` then runs `connect` (`already_connected` with a ready session). Without
    `--claude-code` an upgrade keeps the installed mode. A `transaction_pending` refusal names the
    unfinished action (`install`, `upgrade`, `rollback` or `uninstall`), and uninstall removes only the
    retired root its install intent binds.
  - Owner-pending: the real upgrade and migration of the owner's installation.
- Documentation (phase F): the [install guide](docs/install.md#install-from-the-053-kit-dmg-curl-and-plugin-development)
  covers the DMG, curl and plugin paths, Claude Desktop, upgrade, rollback, uninstall and `--adopt-legacy`;
  the [security model](docs/security.md#signing-and-team-id-trust-chain-053-candidate) and
  [reference](docs/reference.md#client-distribution-reference-053-candidate) have the matching sections,
  and the setup skill describes the new routes.

## 0.52.0 — 2026-10-20

QR-first onboarding, forward/copy, native video, archive/unarchive and own-text edit
([release notes](docs/release-notes-0.52.0.md)). The real QR-first acceptance on the owner's Mac is
recorded below, along with real approved forward/copy, native video, archive/unarchive and own-text edit
runs. A clean macOS user installing the bundle that carries the application identity is deferred to the
backlog ([phase F record](docs/plans/0.52-qr-onboarding-spec.tasks.md)). The owner reviewed the delegated
F14/F15a/F17a decisions on 20.10.2026. The release is an annotated tag with release-gate evidence; the
private bundle is not distributed.

- Apply Telegram's own text rewrite before every preview (owner decision): text sends, replies, topic
  sends, captions and own-text edits share one normalization.
  - A tab becomes a space; U+2028, U+2029, U+030A, U+0333 and U+033F are removed (with NFKC repeated
    until stable); leading and trailing spaces and line feeds are trimmed, exactly the characters Telegram trims.
  - The reviewed text is now the stored text. Edits no longer refuse such texts, and sends no longer
    send a text that Telegram silently changes.
  - Style span offsets refer to this final text.
- Require `read` for `archive_chats` and `edit_own_text` (owner decision): their prepare step reveals an
  own message's text or a chat's title and list, so these narrow reads now need `read`. `read` is on by
  default, so a normal setup is unaffected. A refusal names the missing `read`.

- Real acceptance on the owner's Mac, 20.10.2026 (owner's macOS user, source install, Keychain
  identity): QR login, refresh, page Cancel and screen expiry; a 2FA login with the password typed
  only on the page; phone revoke ⇒ `AUTHORIZATION_BLOCKED`, then `connect` reconnects on the same
  enrollment; repeat `connect` ⇒ `already_connected`. The default policy is exactly `read` and
  `artifacts`. Bounded own-chat search worked through the official MCP SDK client, real Codex
  (`codex exec`) and a new Claude Code session.
- Fix Keychain reads that failed on every real Mac (`-50`): count matching items first, then read
  exactly one item's data. An item removed between the two queries reads `unavailable`.
- Wait for a starting broker instead of restarting it: a client request no longer runs
  `launchctl kickstart -k`, which killed a broker in the middle of TDLib startup. It starts a
  stopped agent and waits up to 10 seconds for it.
- Ask TDLib which lists a chat may join before an archive draft (`getChatListsToAddChat`): an
  unsupported move is refused as `unsupported_chat` with no draft or dialog. A chat a fresh session
  has not loaded yet is loaded once from Main and Archive.

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
  forward drafts. A real approved copy and forward to Saved Messages were sent and read back on
  20.10.2026, through the official MCP SDK client on the owner's account. See [reference](docs/reference.md#exact-forwardcopy-f14) and
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
  A real approved video send was confirmed and read back on 20.10.2026: a 3-second H.264 MP4
  with its caption. See [reference](docs/reference.md#native-video-send-f15a) and
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
  provider error is `failed`. The new text gets the shared normalization, which applies Telegram's own
  rewrite before the preview (see below); sponsored or announcement list positions are not list membership. The `_manifest`
  `forbidden` list now names exactly these two mutations instead of all edits. `get_send_status` reads
  coarsely and never `sent` for these drafts, the generic draft tools do not expose them, and because
  each prepare tool reveals that chat's or message's content, both capabilities also require `read`. The earlier 51
  tool schemas are unchanged. A real own-text edit and a real archive and unarchive of a supergroup
  were applied and read back on 20.10.2026. See [reference](docs/reference.md#archiveunarchive-and-own-text-edit-f17a)
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
  Real acceptance on the owner's Mac is the first entry above. The clean macOS user run is in the backlog.

- Report SDK guard refusals with a fixed reason instead of the bare `Error executing tool <name>`:
  `capability_disabled` names only the missing capabilities (for example `read_history`, or
  `reply_text_send, send`), and `config_stale` and `client_schema_mismatch` carry a fixed hint.
  No input or provider text is echoed; local upload authority tools still return `unavailable`.
  See [reference](docs/reference.md#runtime-contract-and-compatibility).

- Record the approved [QR-first onboarding requirement](docs/plans/0.52-qr-onboarding.md)
  as the next priority and a mandatory 0.52.0 release gate. Application-supplied
  API credentials replace mandatory end-user API application creation/entry in
  the target flow. This entry records the decision; the implementation is the entry
  above.

- Add authenticated offline macOS ARM64 first-install bundle admission,
  recoverable owned runtime/registration/paired-skills installation and an
  isolated receipt-bound launcher integrated with the preserved default session.
- Private installation ends at `activation_required`. Still open: installing the bundle on a clean
  macOS user, public distribution (Developer ID signing and notarization) and source completeness.

- Preserve client-owned artifact grants across the supported local lifecycle and
  add retained selections for bounded conversational message reuse.
- Add issuer-owned local upload authorities, stdin-only CLI handoff and original
  client result retrieval/cancellation, including idempotent receipt recovery after
  a lost response without copying bytes or renewing storage.
- Verify the bounded B2C slice with the full guarded suite, package/install checks,
  independent review and installed official SDK auto/legacy scenarios using
  synthetic data. No real Telegram send or full F18 acceptance is claimed.
- Development was paused after B2C at the owner's request and later resumed (QR onboarding,
  F14, F15a and F17a above); the unfinished scope and evidence limits are in
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
