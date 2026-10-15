# Development progress and roadmap

[Overview](../README.md) · [Original plan](plans/initial-roadmap.md) · [0.51.0 release notes](release-notes-0.51.0.md) · [Licensing](licensing.md)

Updated **2026-10-16**. The released baseline remains **0.51.0**; the current
**0.52.0.dev0** development snapshot and pause boundary are recorded below.
The [original plan](plans/initial-roadmap.md) and historical 0.51.0 table remain
preserved. Technical acceptance of a bounded slice does not complete that plan.

## Current development snapshot — 0.52.0.dev0

Development is paused after the **B2C local-upload ownership slice**. No next
implementation batch is started. This is development source, not a new tagged
release, installer distribution or PyPI publication. Resume requires an explicit
request; the remaining scope below is retained.

**Approved 0.52 requirement — next priority #1:**
[QR-first Telegram onboarding](plans/0.52-qr-onboarding.md). The normal user path
must be install MCP + skills → local QR screen → confirmation in the phone's
Telegram app → optional local 2FA → ready MCP and a bounded Codex read/search.
End users must not create API applications or enter API ID/hash; the distributed
application supplies its API identity. This is mandatory for 0.52.0. It supersedes
conflicting manual-credential and Terminal-only onboarding plans; the Terminal
phone/code setup remains only as a fallback.

**0.52 QR-first status** ([spec](plans/0.52-qr-onboarding-spec.md), phases A–F):
phases A–E are technically done on branch `qr-onboarding-052` — transport and credential
resolution, connect flow, loopback QR screen, `connect`/`wait`/`api-credentials` with the
transient launchd job and narrow reconnect, and the installer/docs phase (application
identity injection at assembly, Claude Code and Codex registration, skill copies for both
clients, rewritten setup skill). Evidence is synthetic: fakes for Telegram, launchd, the
`claude` CLI and Keychain. **Phase F (real acceptance) is pending the owner:** a separate
macOS user, a test-account phone scan with 2FA, bounded own-chat search in Claude Code and
Codex, QR refresh/cancel/interruption, restart and revoke/reconnect, and unchanged live 0.51
hashes. Until then 0.52 is not accepted and not released.

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

Still paused and unfinished:

- **First:** accept the mandatory 0.52 QR-first onboarding scope (phase F: real
  clean-user run with a test-account phone, 2FA, reconnect and repeat use); phases A–E
  are technically implemented.
- Installed file/page/parser coverage beyond the bounded B2 message and B2C text
  file scenarios; broader conversational and no-skill consumer validation.
- F13 formatted captions and remaining applicable topic/reply/live scenarios;
  retained TextUrl captions, CustomEmoji, MediaTimestamp and their combinations.
- F14 exact forward/copy; F15 native video and ordered albums.
- F16 isolated account aliases and real two-account validation.
- F17 archive/unarchive, own text/caption editing, pin/unpin and exact deletion.
- Final whole-scope F18 Codex/Inspector/SDK evidence, complete distribution-source
  and notice closure, and the final requirement/privacy/history/package review.

F19 HTTP remains not applicable without a demonstrated cloud-consumer need.
Prepared notes for future slices are planning artifacts, not implemented features.

## Product direction

The goal is convenient conversational interaction: find information or an attachment
by remembered context, inspect it, process it, prepare a result and explicitly send
it to the intended Telegram conversation without manual IDs or file copying.

The next focus is the mandatory QR-first setup and repeated completion of that
path on the supported macOS platform, initially in Codex. Short answers
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
| P2 | Installed public product | Partial | Package build/install and existing-owner scenarios passed; first-time Telegram provisioning for a new user remains unfinished. |
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
| F14 | Forward/copy | Planned | Exact forward/copy semantics, protected-content and source checks, preview and actual approved delivery. |
| F15 | Native video and albums | Planned | Video metadata/support profile, ordered album preview, partial outcomes and real delivery. Sending a document does not count as native video. |
| F16 | Multiple accounts | Planned | Trusted local account registry, isolated ownership and real two-account validation. First-time single-account setup should be completed earlier. |
| F17 | Archive/edit/delete/pin actions | Planned | Separate opt-ins, previews, permissions, final-state checks and per-action real approvals. |
| F18 | Full consumer/install/upgrade matrix | Partial | Current SDK, persistent Inspector and native Codex checks exist; clean-user onboarding, full source-family/protocol/recovery coverage and controlled TDLib upgrades remain. |
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

1. **Priority #1 — QR-first onboarding for 0.52:** follow the
   [approved requirements and acceptance gate](plans/0.52-qr-onboarding.md).
   The installer, application identity, local QR/optional-2FA screen and
   `connect`/`wait` commands are implemented (phases A–E); what remains is to prove a
   clean install → real phone confirmation → Claude Code and Codex search →
   repeat-use path (phase F, owner-run). Mandatory
   end-user API-pair entry or Terminal-only login does not satisfy this scope.
   Resolve incompatible earlier assumptions here before any unrelated B2/F13–F17
   continuation. Public installer distribution, trust and source/notice closure
   remain release dependencies. Development stays paused until explicit resume.
2. **Conversational usability:** keep context for selected conversations/messages/files;
   make core tools understandable without a large optional skill; improve concise
   answer/source presentation and the human approval experience without weakening
   exact-payload authorization. Retained message selections and B2C upload ownership
   now have bounded technical evidence in the development snapshot; full installed
   file/page and conversational outcome coverage remains open.
3. **Blocking send capabilities:** complete F14 forwarding/copying and F15 native video
   when required by observed tasks; each needs its own preview and delivery evidence.
4. **Remaining established backlog:** F16 accounts; F17 archive/unarchive and own
   text/caption editing; F15 albums; retained TextUrl captions, CustomEmoji,
   MediaTimestamp and combinations; F17 pin/unpin and exact selected deletion.
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
