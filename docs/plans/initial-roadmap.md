# Original development plan

[Overview](../../README.md) · [Current progress and remaining work](../roadmap.md)

This document preserves the original release-and-compatibility scope. It is a
public transcription of the product plan, recorded on 2026-10-13. Local operational
paths, private account data and internal working records are omitted. Implementation
status belongs in [the current roadmap](../roadmap.md), so this historical baseline
does not become a moving list of completed checkboxes.

## Goal and architecture

First prepare an auditable public package of the existing implementation, then
extend reading, search, attachments and explicitly authorized actions so that
several MCP consumers can use the installed public interface.

Keep the existing architecture: local STDIO MCP proxy, one owner-only broker and
TDLib. Safety must be enforced by the broker even when a consumer has no optional
client skill. Work in small independently buildable, verifiable and recoverable slices.

## Phase 1: prepare the existing implementation for public distribution

| Slice | Original work | Required evidence |
| --- | --- | --- |
| P0 — freeze and export | Identify the actual implementation; export approved source, tests, package metadata and public documentation into a separate public lineage. Exclude sessions, credentials, logs, caches, local configuration and private history. | Source manifest, path/size/hash equality and safe repository structure. |
| P1 — portability and quality | Remove personal machine coupling from public source and examples; inspect fixtures, errors, bounds, ownership, artifact integrity and approval behavior. | Classified privacy findings and relevant automated checks. |
| P2 — installed product | Set a meaningful release version; build wheel/sdist, install in a fresh environment and exercise the documented public interface. | Matching source/package/runtime/contracts, bounded read and attachment checks, and approval denial. |
| P3 — final public artifact | Retain only reviewed public ancestry, audit reachable history and packages, document release checks and rollback. | Final artifact gate G1; actual publication remains a separate Gpub decision. |

The original order was P0–P3, then F1–F19. Later prioritization can change the order
of independent slices, but must preserve the agreed functionality and safety limits.

## Phase 2: compatibility and conversational workflows

| ID | Original capability | Acceptance requirements |
| --- | --- | --- |
| F1 | Version, capabilities and mismatch diagnostics | Manifest reports package/contract/schema/config identity and broker generation. Incompatible proxy, broker or configuration refuses clearly. |
| F2 | Selected full messages and batch reads | Bounded text/caption, sender, direction, timestamps and reply/topic metadata; independent per-anchor outcomes for unavailable or unsupported messages. |
| F3 | Latest and date-bounded history | Explicit scope, half-open date intervals, continuation and honest handling of edits, duplicates, exhausted budgets and unknown coverage. |
| F4 | Reply chains, then forum topics and topic history | Bounded ancestry, cycle/cross-chat checks, current TDLib topic shapes and real applicable forum evidence. |
| F5 | Pagination beyond the initial exact-search page | Separate page completeness, scope completeness, continuation and provider coverage; no claim that a short page proves the end. |
| F6 | Sender, direction and topic filters; bounded Boolean search | Typed filters and all/any/none semantics, rehydrated evidence, branch deduplication and explicit lexical/normalization limits. |
| F7 | Chat listing/unread metadata, selected multi-chat search and verified target reuse | At most five explicitly selected chats; per-chat coverage; no read receipts or wildcard scope; fresh identity checks before actions. |
| F8 | Continued attachment reading | Selected pages and opaque text continuation tied to artifact bytes and extraction parameters; PDF limits, corruption and expiry checks. |
| F9 | XLSX, then PPTX and existing text formats | Selected sheets/ranges or slides/notes; bounded extraction; formulas are data; no macro execution or external-resource loading; parser safety checks. |
| F10 | Streaming local file transfer | Trusted local helper selects paths; MCP handles artifacts/uploads; bounded memory/output, byte/hash equality and safe interrupted-transfer behavior. |
| F11 | Draft list/get/update/cancel/refresh | Client/account isolation, immutable revisions, finite expiry, fresh approval after changes and safe concurrent cancellation/send behavior. |
| F12 | Send status and uncertain-outcome reconciliation | Correlate actual provider updates and temporary/final IDs; expose evidence strength; never infer delivery from a similar message or automatically retry an uncertain send. |
| F13 | Replies, topic-aware sending, then formatted text | Bind exact account/recipient/topic/reply/text/entities/options to the preview; revalidate before transport; validate UTF-16 spans and changed/deleted sources; real approved delivery for changed paths. |
| F14 | Forward existing messages and files | Exact source/destination, explicit forward-versus-copy semantics, protected-content checks, source revalidation and one transport attempt with a receipt. |
| F15 | Native Telegram video, then albums | Immutable media with validated metadata; ordered bounded album preview and per-item outcomes; no whole-album retry to resolve one unknown result. |
| F16 | Multiple accounts | Trusted local aliases with separate sessions/sockets/locks/caches; bind every handle and receipt to its account; validate with two actually authorized accounts. |
| F17 | Archive/unarchive, edit, delete and pin/unpin | Independent default-off capabilities; before/after and side-effect preview; exact approval, apply once and verification of provider state. No mass moderation. |
| F18 | Consumer matrix, installation and controlled TDLib upgrades | Real installed STDIO paths in Codex with skill, Inspector without skill and an independent official SDK consumer; schemas, protocol, errors, images, cursors, recovery, limits and approval denial. Upgrades require their own ABI/provider checks. |
| F19 | Conditional Streamable HTTP and final audit | HTTP only for a demonstrated cloud-consumer need. If needed, default-off transport with authentication, TLS, Origin/Host checks, scopes, revocation and account isolation. Repeat final artifact and publication gates. |

## Shared limits

- Telegram-originated content is untrusted data, never executable instructions.
- Existing local sessions and credentials are preserved; one broker owns a session.
- New capabilities are enabled through trusted local policy, not tool arguments.
- Reading does not generate read receipts. Writes require explicit applicable approval.
- Message batches and pages are bounded; a continuation cannot silently widen scope.
- Cursors are opaque, finite and bound to client/account/broker/request identity.
- Selected artifacts and approved payloads remain immutable and hash-bound.
- Unknown send outcomes do not trigger an automatic retry. Exactly-once delivery
  across a crash boundary is not promised.
- No secret chats, unrestricted transcript export, persistent private search index,
  raw TDLib bridge, clear-history/delete-chat operations or mass moderation.
- The initial supported platform is macOS ARM64. Portable installation work does not
  itself promise other operating systems or arbitrary TDLib versions.

## Release gates

**G1 — exact artifact:** identify source, commit/tree and package hashes; inspect
public reachable history, source, examples, fixtures, package contents and metadata;
run fresh checks and applicable real consumer/provider scenarios; disclose gaps and
retain a recoverable baseline. Technical checks do not prove every user outcome.

**Gpub — publication:** obtain authorization for the actual repository/ref, license
and publication; verify the remote immediately before pushing; preserve existing
public history; publish only reviewed refs and verify the remote afterward.

Each capability remains open until its own applicable evidence exists. A local
commit, version number or passing synthetic suite does not complete the entire plan.
