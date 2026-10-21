# Development progress and roadmap

[Overview](../README.md) · [Original plan](plans/initial-roadmap.md) · [0.51.0 release notes](release-notes-0.51.0.md) · [Licensing](licensing.md) · [Releasing](releasing.md)

Updated **2026-10-22**. The released baseline remains **0.51.0**; the
**0.52.0** release is recorded below.
The [original plan](plans/initial-roadmap.md) and historical 0.51.0 table remain
preserved. Technical acceptance of a bounded slice does not complete that plan.

## In development — **0.53.0.dev0**

Client coverage and distribution: [spec](plans/0.53-client-distribution-spec.md) and its [task record](plans/0.53-client-distribution-spec.tasks.md). The earlier `0.53-*` and `0.54-*` specs (forward/copy, native video, archive and edit) shipped in 0.52.0.

**Implemented, with technical evidence only** (unit tests, synthetic kits, and an ad hoc dry run on the
build Mac; no signed build and no real run in Claude Desktop or the plugin yet):
- **A. Product identity:** every user-visible name comes from one module, with a rename-impact test and
  a check that every plugin tool name fits 64 characters. TDLib reports the product's device model and
  the package version.
- **B. Reproducible payload:** the native lock and the pruned payload are rebuilt from pinned inputs;
  two builds are identical. Manifest v2 and lock v2 add a reviewed pin table and a Team ID check.
- **C. Signing and distribution scripts:** inside-out signing under a proven entitlement set, seed
  signing with `--seed-team`, the notarization and DMG script (mocked in tests), `Install.command` with
  an inline `connect`, `release_build.py`, the signing preflight and the release gate's bundle stage.
  The ad hoc dry run built a kit and a verified DMG.
- **D. Clients:** Claude Desktop registration, `--claude-code user|plugin|skip`, a Claude Code plugin
  with a repository marketplace and a pinned runtime installer, client-neutral server instructions,
  `available_tools`, a policy-filtered `tools/list` and a structural STDIO recorder.
- **E. Upgrade, rollback, uninstall and `--adopt-legacy`:** journaled, gated on pending sends, and
  leaving the session, enrollment and Keychain untouched.
- **F. Documentation:** the [install guide](install.md#install-from-the-053-kit-dmg-curl-and-plugin-development),
  the security model, the reference, the setup skill and the changelog.

**Waits for the owner (phase G):** the signed and notarized build (the Developer ID identity and notary
profile were provisioned on 21.10.2026, preflight ok) and its Gatekeeper observation, including the
unsigned `Install.command` double-click; the application-identity decision; the product name and its single rename commit; the
clean-macOS-user run from a browser-downloaded DMG (QR with 2FA, interruption, logout and login,
searches in Claude Code, Codex and Claude Desktop, and the plugin path); migration of the owner's daily
install; publication. An independent critic review of the slice ran on 21.10.2026 and its findings were
addressed on 22.10.2026; what stays open is listed in the [task record](plans/0.53-client-distribution-spec.tasks.md).
No signed DMG and no public release asset exists.

## Current release — 0.52.0

The **0.52.0** release contains QR-first onboarding and
three opt-in action slices: F14 forward/copy, F15a native video, and F17a archive/unarchive with own
text edit. All four have real owner-approved evidence from 20.10.2026. It is an annotated tag with
release-gate evidence ([release notes](release-notes-0.52.0.md)); it is not an installer distribution or
a PyPI publication.

**Approved 0.52 requirement:** [QR-first Telegram onboarding](plans/0.52-qr-onboarding.md).
- **The normal user path:** install MCP + skills, then a local QR screen, confirmation in the
  phone's Telegram app, optional local 2FA, and a ready MCP with a bounded read/search in Claude
  Code and Codex.
- **No end-user API application:** end users do not create an API application or enter an API
  ID/hash; the distributed application supplies its API identity.
- **Fallback:** the Terminal phone/code setup remains only as a fallback.

**0.52 QR-first status** ([spec](plans/0.52-qr-onboarding-spec.md),
[phase F record](plans/0.52-qr-onboarding-spec.tasks.md)): phases A–E are implemented.

Phase F was accepted for real on the owner's Mac on 20.10.2026, on the owner's macOS user with a
source install, the Keychain identity and two Telegram accounts:
- **Login:** QR login and handoff, an observed QR refresh, page Cancel, screen expiry, and a 2FA
  login with the password typed only on the page.
- **Recovery:** a phone revoke stops the broker with `AUTHORIZATION_BLOCKED`, and `connect` then
  reconnects on the same enrollment. A repeat `connect` reads `already_connected`, and a broker
  restart needs no new login.
- **Policy and clients:** the default policy is exactly `read` and `artifacts`. A bounded own-chat
  search worked through the official MCP SDK client, real Codex (`codex exec`) and a new Claude
  Code session.
- **Defects found and fixed:**
  - Keychain reads failed on every real Mac (`-50`);
  - a starting broker was killed by `launchctl kickstart -k`;
  - archive drafts were accepted for chats TDLib cannot move or has not loaded;
  - a disabled capability reached the client as a bare error and now names the missing capability.
- **Bundle:** a bundle carrying the owner's application identity is assembled and verified offline
  with `bootstrap.sh --prepare-only`. It has not been installed yet.

**Owner review done** on 20.10.2026: the delegated F14, F15a and F17a decisions are accepted. Two
behaviour changes came out of it:
- Telegram's own text rewrite is applied before every text, caption and edit preview, so the reviewed
  text is the stored text.
- `archive_chats` and `edit_own_text` also require `read`.

**Release:** 0.52.0 is cut as an annotated tag with release-gate evidence; publishing `main` and the tag waits for the owner's history decision (see the [completion plan](plans/completion-plan.md)).

**Moved to the backlog** by the owner on 20.10.2026: installing that bundle on a clean macOS user and
connecting there. A kit is ready. Until that run happens, the bundle path is verified offline only.

**F14 exact forward/copy** ([spec](plans/0.53-forward-copy-spec.md)):
- **What it does:** `prepare_forward_send` and `send_prepared_forward` forward, or copy without
  attribution, exactly one message between two exact chats, behind the default-off `forward_send`
  capability (together with `send`).
- **Real evidence:** an approved copy and forward to Saved Messages were sent and read back through
  the official MCP SDK client.
- **Pending:** a visual check of the forward header in a Telegram app.

**F15a native video** ([spec](plans/0.53-native-video-spec.md)):
- **What it does:** `prepare_video_send` and `send_prepared_video` send one owned MP4/MOV of at most
  64 MiB as a native video, behind the default-off `video_send` capability (together with `send`).
  Metadata is read locally from the file's own boxes.
- **Real evidence:** a 3-second H.264 MP4 was sent with its caption and read back.
- **Accepted limits:** the provider lock is held during the upload wait, and later calls after lost
  authority still read `expired`.
- **Not started:** albums (F15b).

**F17a archive/unarchive and own text edit** ([spec](plans/0.54-archive-and-edit-spec.md)):
- **What it does:** four tools behind the independent default-off `archive_chats` and
  `edit_own_text` capabilities (each together with `send` and `read`).
- **Real evidence:**
  - an own-text edit was applied and read back;
  - a supergroup was archived and unarchived, with the target list observed each time;
  - Telegram's refusal to archive the service chat surfaced as `failed`, with no retry.
- **Recorded residual:** a concurrent caller can read `outcome_unknown`. The informational reads and the
  TDLib text rewrite were closed by the owner review.
- **Not started:** captions, styled edits, pin/unpin and deletion.

The local transfer CLI can now consume a short-lived upload authority issued by
the preparing MCP client. The helper returns a result reference; the original
client consumes it to obtain its own artifact grant. A copied result reference
does not grant another client access. Retrieval can recover a completed upload
or a consumed receipt after a lost response without copying bytes, issuing a new
grant or renewing the original storage deadline. Cancellation and malformed
input retain explicit refusals. Transfer alone does not authorize a Telegram send.

The earlier development slices provide admitted macOS bundle installation,
client-owned artifact lifecycle checks and retained message selections. B2C adds
`issue_local_upload_authority`, `consume_local_upload_result` and
`cancel_local_upload_authority`, plus the CLI `--upload-authority-stdin` path.
Existing legacy transfer behavior remains supported within its documented scope.

Validation for this bounded code snapshot includes independent product review,
the guarded full suite (2,056 parent cases and 6,136 subtests, no failures), package
integrity and private first/repeat installation checks. Installed official SDK
consumers exercised both auto and legacy modes without a skill: local upload and
bounded reading, owner/foreign-client boundaries, cancellation, malformed input,
lost-response recovery, retained-file reuse and an unsent artifact preview.
These checks used synthetic files and an inert provider where preview preparation
required one. They do not establish real Telegram delivery, native onboarding,
all attachment formats or the whole F18 consumer matrix. Earlier failed test
packets remain failed evidence; they are not reclassified as successful runs.

Still unfinished:

- **First (0.52.0):** the version and the tag.
- **Backlog (deferred by the owner on 20.10.2026):** the clean macOS user install of the identity
  bundle, including an interrupted helper and a logout/login on that user.
- **In development as 0.53.0.dev0 — client coverage and distribution** (chosen by the owner on
  20.10.2026; status above):
  - registration in Claude Desktop (implemented, synthetic evidence);
  - a Claude Code plugin package (implemented, synthetic evidence);
  - Developer ID signing and notarization of the bundle (scripts and an ad hoc dry run exist; the signed
    build waits for the owner);
  - a distinct product name before any public listing, because another vendor's directory listing
    already uses "Unofficial Telegram MCP" (open).
- Conversational usability, and installed file/page/parser coverage beyond the bounded B2 message
  and B2C text file scenarios; broader conversational and no-skill consumer validation.
- F13 formatted captions and the remaining applicable topic/reply/live scenarios; retained TextUrl
  captions, CustomEmoji, MediaTimestamp and their combinations.
- F15b ordered albums.
- F16 isolated account aliases and real two-account validation.
- F17 caption and styled edits, pin/unpin and exact deletion.
- Final whole-scope F18 Codex/Inspector/SDK evidence, complete distribution-source and notice
  closure, and the final requirement/privacy/history/package review.

F19 HTTP remains not applicable without a demonstrated cloud-consumer need.
Prepared notes for future slices are planning artifacts, not implemented features.

## Product direction

The goal is convenient conversational interaction: find information or an attachment
by remembered context, inspect it, process it, prepare a result and explicitly send
it to the intended Telegram conversation without manual IDs or file copying.

After the 0.52.0 gate, the next focus is client coverage and distribution (in development as
0.53.0.dev0): the same QR-first
setup in Claude Desktop and as a Claude Code plugin, from a signed and notarized bundle. Short answers
with usable sources, retained conversational selections, understandable approvals
and recovery take priority alongside capabilities that actually block a task.
Broader client/platform support must be demonstrated separately.

The project is intended to remain free to use and open source. Version 0.51.0 uses
AGPLv3 because of its current PyMuPDF dependency. A future PDF replacement and
different license are being considered, with no committed migration date; see
[the licensing rationale](licensing.md).

## Status vocabulary

- **Implemented:** present in 0.51.0 within documented bounds, with technical checks.
  This does not assert exhaustive live coverage of every content family or client.
- **Partial:** a useful subset is implemented or accepted; named remaining work exists.
- **Planned:** not implemented in this release.
- **Conditional:** requires a demonstrated need before implementation.

## Original scope versus current state

| ID | Scope | 0.51.0 status | What remains |
| --- | --- | --- | --- |
| P0/P1/P3 | Public export, privacy/history review and release packaging | Implemented for the selected public source line | Repeat audit for future source/history/artifact changes. Private development history is not part of the public ancestry. |
| P2 | Installed public product | Partial (0.52 candidate: QR-first first-time provisioning accepted for real on the owner's Mac, 20.10.2026. 0.53 candidate: reproducible payload, signing and notarization scripts, a DMG with `Install.command`, Claude Desktop registration, the Claude Code plugin, and upgrade, rollback and uninstall are implemented and tested with synthetic kits and an ad hoc dry run) | Owner-run: a Developer ID signed and notarized build, installing it on a clean macOS user from a downloaded DMG (backlog, deferred by the owner on 20.10.2026), migration of the owner's installation, and public hosting. Also a distinct product name before a public listing, and source and notice closure. |
| F1 | Version/capabilities/compatibility | Implemented | Broader protocol/upgrade compatibility remains part of F18. |
| F2 | Selected message and batch reads | Implemented | Retain unsupported-type and per-message coverage limits. |
| F3 | Bounded latest/history | Implemented | The pinned provider path cannot certify complete history coverage; this is an explicit limitation. |
| F4 | Reply ancestry, topics and topic history | Implemented | Broader source-family and topic-state live coverage remains scope-specific. |
| F5 | Paginated exact search | Implemented | Lexical recall and provider coverage are not guaranteed semantic recall. |
| F6 | Typed filters and bounded Boolean queries | Implemented | Preserve per-branch coverage and documented normalization limits. |
| F7 | Chat listing/unread, selected multi-chat search and target reuse | Implemented | Keep explicit selected scopes and revalidation; no unrestricted whole-account content export. |
| F8 | Attachment pages and text continuation | Implemented | Supported extraction limits remain; arbitrary document fidelity is not promised. |
| F9 | Selected XLSX/PPTX and text readers | Implemented | No formula execution, macros or external resource loading; unsupported objects remain explicit. |
| F10 | Local streaming transfer | Implemented | Paths stay on the trusted local helper surface; content transfer is not a Telegram delivery receipt. |
| F11 | Draft inspection/revision/cancel/refresh | Implemented | Drafts are finite and client-scoped; reconnect/restart can invalidate state. |
| F12 | Send-status inspection/correlation | Implemented | Unknown after state loss remains unknown; no automatic send retry or crash-boundary exactly-once promise. |
| F13 | Replies, topic sends and outgoing styles | Partial | Plain/topic/reply paths and five text styles exist; retained rare source entities, formatted captions and applicable remaining live scenarios are not complete. |
| F14 | Forward/copy | Partial (0.52 candidate): one-message forward/copy with real owner-approved copy and forward, read back through the official MCP SDK client (20.10.2026) | Visual check of the forward header in a Telegram app. Multiple messages and albums (F15), topic destinations, retained-selection sources and caption editing are out of scope. |
| F15 | Native video and albums | Partial (0.52 candidate): one native video (F15a) with a real owner-approved send, read back (20.10.2026); albums (F15b) planned | Ordered albums (F15b) with partial outcomes. Videos over 64 MiB, replies, topic destinations, thumbnails and transcoding remain out of scope. Sending a document does not count as native video. |
| F16 | Multiple accounts | Planned | Trusted local account registry, isolated ownership and real two-account validation. First-time single-account setup should be completed earlier. |
| F17 | Archive/edit/delete/pin actions | Partial (0.52 candidate): archive/unarchive of one chat and plain-text edit of one own message (F17a), with real owner-approved runs read back (20.10.2026) | Caption edits, styled edits, pin/unpin and deletion each still need separate opt-ins, previews, permissions, final-state checks and per-action real approvals. Folders, bulk operations and other people's messages are out of scope. |
| F18 | Full consumer/install/upgrade matrix | Partial | Current SDK, persistent Inspector and native Codex checks exist. The 0.52 candidate adds real Codex (`codex exec`) and Claude Code searches on the owner's Mac. The 0.53 candidate adds Claude Desktop registration, the plugin, a policy-filtered `tools/list` and upgrade, rollback and uninstall of the installed bundle, with synthetic and ad hoc evidence only. Still open: clean-user onboarding, real Claude Desktop and plugin runs, a real upgrade and rollback on an installed bundle (the owner's migration), full source-family/protocol/recovery coverage and controlled TDLib upgrades. |
| F19 | Streamable HTTP and final whole-scope audit | Conditional / partial | No current confirmed cloud-consumer requirement: HTTP is not applicable now. Final full-plan audit remains after the applicable backlog. |

## Completed release slices

| Version | Delivered slice | Evidence boundary |
| --- | --- | --- |
| 0.35.0 | Previous public baseline and rebrand | Historical public source release. |
| 0.48.0 | Additional inert hidden-link evidence in message text | Technical source/package/SDK acceptance; not a general URL fetch or caption capability. |
| 0.49.0 / 0.49.1 | Exact forum-topic text sending; numeric-schema portability and changed-authority refusal fixes | Bounded technical acceptance, followed by applicable current-release consumer checks. |
| 0.50.0 | Topic-aware text replies | Implemented within documented source and topic admission bounds. |
| 0.51.0 | Five explicit outgoing text styles, normalization checks, package preflight diagnostics, corrupt-PNG handling and Saved Messages receipt correction | Full automated suite and package closure; real SDK/Inspector/native checks of the accepted bounded scope. |

Intermediate versions were local development milestones. This table does not
assert that every intermediate version was separately released publicly.

## Accepted 0.51.0 scenarios and limits

The corrected source passed its automated suite, source/package/install comparison,
installed RECORD checks and independent review. Genuine official SDK and persistent
Inspector consumers checked public schemas, reads/search/continuation, Unicode,
image blocks and damaged-image recovery, draft lifecycle, styles, topic paths,
approval denial and schema-mismatch recovery.

Native Codex checked a selected real Telegram text attachment and both reader paths.
A separately approved corrected Saved Messages send had a correlated provider
confirmation and exact readback, including independent SDK verification. A forum
test send also had a provider-confirmed receipt and exact readback.

These facts do **not** establish every style's visual rendering, every supported
content family, first-time-user setup or unattended operation. A historical unknown
send stays unknown. The source of a later test reply had been deleted; its safe
refusal was accepted, and no new reply delivery is claimed for that deleted source.

## Remaining work and approved priority

1. **Priority #1 — release 0.52.0:** follow the
   [approved requirements and acceptance gate](plans/0.52-qr-onboarding.md).
   - **Done:** QR-first onboarding is implemented (phases A–E) and accepted for real on the owner's
     Mac, including 2FA, revoke/reconnect and Claude Code and Codex searches (phase F, most of it).
   - **Remaining:** the version and the tag. The owner reviewed the delegated F14/F15a/F17a decisions
     on 20.10.2026. The clean macOS user install is in the backlog (deferred by the owner on the same
     day).
   - **Out of scope:** mandatory end-user API-pair entry or Terminal-only login does not satisfy
     this scope.
2. **Priority #2 — client coverage and distribution (in development as 0.53.0.dev0):** the same
   QR-first setup registered in Claude Desktop and installable as a Claude Code plugin. This includes
   Developer ID signing and notarization of the bundle so that a downloaded copy passes Gatekeeper, a
   distinct product name before any public listing, and public hosting with source/notice closure.
   Implemented and tested synthetically: the registrations, the plugin and the scripts. Waiting for
   the owner: the signed build, the clean-user run, the migration, the name and publication (see the
   status above).
3. **Conversational usability:** keep context for selected conversations/messages/files;
   make core tools understandable without a large optional skill; improve concise
   answer/source presentation and the human approval experience without weakening
   exact-payload authorization. Retained message selections and B2C upload ownership
   have bounded technical evidence; full installed file/page and conversational outcome
   coverage remains open. Guard refusals already name the missing capability.
4. **Remaining established backlog:** F16 accounts; F15b albums; F17 caption and styled edits,
   pin/unpin and exact selected deletion; F13 retained TextUrl captions, CustomEmoji,
   MediaTimestamp and combinations. Each needs its own preview and real evidence.
5. **Final acceptance:** close the applicable whole-scope F18 matrix, package/history/
   privacy/license checks and requirement-by-requirement review before claiming the
   complete original plan is finished. Reassess F19 only if a cloud need appears.

The source release can be useful before the full roadmap is complete. Deferred
items are retained here, not silently removed from the agreed scope.

## Validation proposal

A small pilot should compare a new user's setup and find-document → process →
review → send task against ordinary Telegram plus copy/paste, then observe repeat
usage. Time, manual transitions, clarifications, correct sources, recipients and
completed outcomes matter more than tool or test counts. Five participants over
two weeks is a proposed learning experiment, not a completed study, launch date
or commitment to market-wide readiness.

## Maintaining this record

Update the relevant row when a slice is implemented, record the release containing
it and distinguish automated/installed-consumer evidence from actual provider and
user outcomes. Keep [the original plan](plans/initial-roadmap.md) as the baseline;
use Git history to follow later scope, status and priority changes.
The release procedure, gate and publication checks are in [releasing.md](releasing.md).
