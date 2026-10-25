# Final acceptance — requirement-by-requirement audit

**Status:** lead record, 25.10.2026, final acceptance criterion 5 ([spec](final-acceptance-spec.md),
[task record](final-acceptance-spec.tasks.md)). Written on the `final-acceptance` branch at the 0.58.0rc1
candidate plus the TDLib upgrade procedure (version 0.59.0.dev0). **The independent critic of this table has not
run yet**; until it has, criterion 5 stays open. The owner's decisions and the owner-session rows are consolidated
in the [owner decision sheet](owner-decision-sheet.md).

Baseline: every requirement of the [original plan](initial-roadmap.md) (P0–P3, F1–F19, the shared limits and the
release gates), the [completion plan](completion-plan.md), the [roadmap](../roadmap.md), every spec and task record
from 0.52 to final acceptance, the release notes and the changelog. Rows that depend on behaviour were checked
against the code at this branch.

## How to read this table

- **Evidence classes.** *Technical*: unit, synthetic, transport, socket and golden tests, in-process official SDK
  clients and the release gate. *Consumer*: an installed official SDK or a real MCP client over STDIO (the consumer
  matrix installs the wheel built from `git archive`; the conversation runs use real `codex exec` against the real
  server on synthetic data). *Real*: a real Telegram account, with the owner's approval where something changes.
  Synthetic evidence is technical evidence only; an unknown outcome stays unknown.
- **met:** the requirement's acceptance is covered by technical and consumer evidence, and by real evidence where
  the requirement or `CLAUDE.md` asks for it; nothing owner-only is pending for it.
- **met with owner remainder:** technically complete, and everything an agent can produce exists; the evidence still
  missing can only come from the owner session. The row closes only when those rows pass.
- **partly met:** a part of the requirement lacks evidence that an agent can still produce, or a part is not yet
  done.
- **not applicable / not met:** no row has either status. F19's HTTP part is not applicable inside a partly met row.
- **Which code the evidence is for.** A record is evidence for its own commit. Real runs happened on 0.51.0 (the
  owner-accepted public release; its per-capability acceptance records are outside the public tree and the
  [0.51.0 notes](../release-notes-0.51.0.md#accepted-scenarios) name them only in summary) and on 0.52 source
  installs (20.10.2026). Later candidates carry technical evidence and the consumer-matrix receipt of their own
  commit. A row records whether the capability was accepted with its applicable evidence; re-running real scenarios
  on the final candidate is part of the final G1 (P3, F19), not of each row.
- **Tags.** Local annotated tags, none pushed: `v0.51.0` (published baseline), `v0.52.0`, `v0.53.0rc2` (replaces
  `v0.53.0rc1`), `v0.54.0rc1` … `v0.58.0rc1`. Each later tag contains the earlier ones. The final-acceptance work
  (TDLib procedure, this audit) has no tag yet.

## Summary

| Status | Count | Rows |
| --- | --- | --- |
| met | 12 | P0, P1, F1, F2, F3, F4, F5, F6, F7, F10, F11, F12 |
| met with owner remainder | 4 | F8, F9, F14, F16 |
| partly met | 7 | P2, P3, F13, F15, F17, F18, F19 |
| not applicable | 0 | (F19's HTTP part is not applicable; the row is partly met) |
| not met | 0 | — |

No requirement lacks evidence altogether. Three of the partly met rows (F13, F15, F17) wait on the same agent-side
item, the consumer-matrix cells for the 0.56–0.58 tools (F18, in progress on another branch); once those cells pass,
they become met with owner remainder unless the cells show a defect. P2, P3, F18 and F19 also have agent-side items
(open items 2–5). The complete original plan is **not** finished: every partly met row and every owner remainder
below is open.

## Requirements

| ID | Requirement | Status | Evidence | Owner-only remainder |
| --- | --- | --- | --- | --- |
| P0 | Freeze and export: identify the implementation and export approved source, tests, metadata and public docs into a separate public lineage, without sessions, credentials, logs, caches, local configuration or private history. | met | `v0.51.0`, the public lineage ([0.51.0 notes](../release-notes-0.51.0.md)). Since `v0.52.0` the release gate re-derives commit, tree, sdist and wheel hashes from `git archive` and requires the sdist to hold exactly the tracked public files (`packaging/release_gate.py`; `tests/test_release_gate.py`, `tests/test_release_package.py`, `tests/test_release_evidence.py`); `docs/plans/*` other than the original plan is internal and never shipped. | None for P0; the history of the later range is P3. |
| P1 | Portability and quality: no personal machine coupling in public source and examples; fixtures, errors, bounds, ownership, artifact integrity and approval behaviour inspected. | met | The privacy guard over every tracked file with a classified allowlist (`tests/test_public_tree_privacy.py`), also run by every gate over the sdist, the wheel, the tip diff and the commit messages (0 hits on `v0.52.0` and every candidate). No path, label or socket default outside `config.py` (`tests/test_account_paths_sweep.py`, `v0.55.0rc1`); one product-identity module (`tests/test_product_identity.py`, `v0.53.0rc2`); fixed, input-free refusal texts (`tests/test_guard_refusals.py`, `tests/test_surface_054.py`); bounds, ownership and approval per F-row below. | None. |
| P2 | Installed product: a meaningful version; wheel and sdist built and installed in a fresh environment; the documented public interface exercised. Widened by the [0.52 QR requirement](0.52-qr-onboarding.md) (QR-first onboarding of the distributed bundle on a clean user) and by [0.53](0.53-client-distribution-spec.md) (signed DMG, Claude Desktop, plugin, upgrade, rollback, uninstall). | partly met | Fresh install of the `git archive` wheel with the pinned closure, bounded reads, image and PDF blocks and approval denial: consumer-matrix receipts, 32/32 synthetic cells, on `v0.54.0rc1` to `v0.58.0rc1` (`packaging/release_consumer_matrix.py`, `tests/test_release_consumer_matrix.py`). QR onboarding phases A–E (`tests/test_qr_auth_transport.py`, `tests/test_connect_flow.py`, `tests/test_connect_server.py`, `tests/test_connect_cli.py`, `tests/test_credential_identity.py`) and real phase F on the owner's own macOS user from a source install: QR login and handoff, an observed refresh, page Cancel, expiry, 2FA on the page, revoke and reconnect, restart without a login, default policy `read` + `artifacts`, searches through the official SDK, `codex exec` and Claude Code ([0.52 task record](0.52-qr-onboarding-spec.tasks.md), `v0.52.0`). 0.53 distribution, technical: two identical payload builds, an ad hoc signed dry run, journaled installer with kill sweep and seeded random sequences (`tests/test_prepare_payload.py`, `tests/test_sign_payload.py`, `tests/test_release_build.py`, `tests/test_install_command.py`, `tests/test_plugin_install.py`, `tests/test_bundle_claude_desktop.py`, `tests/test_bundle_upgrade.py`, `tests/test_bundle_kill_sweep.py`, `tests/test_bundle_random_sequences.py`); the gate's bundle stage on `v0.53.0rc2` ran the kit's own seed on its index. **Agent-side gap:** no mounted-DMG or `Install.command --prepare-only` run exists for any candidate after 0.53.0.dev0 ([rc2 record](0.53-client-distribution-spec.tasks.md#release-candidate-0530rc2-22102026)). | The signed and notarized build and its observations ([owner checklist](0.53-signing-owner-checklist.md), sections 5–7); the clean macOS user run from a browser-downloaded DMG with no developer tools (QR with 2FA, interruption and resume, logout or login without a new login, repeat install, searches in Claude Code, Codex and Claude Desktop, the plugin path), which are the 0.52 gate's unmet bullets; the API identity of the public bundle; the product name; migration of the owner's own install. |
| P3 | Final public artifact: only reviewed public ancestry; reachable history and packages audited; release checks and rollback documented; gate G1 on the final artifact; publication a separate Gpub. | partly met | [Releasing](../releasing.md) (gate, privacy and history scans, identity report, Gpub, rollback) and [security: rollback](../security.md#rollback); `release_gate.py run` and `verify` passed on `v0.52.0` and every candidate (see Release gates). History findings are reported per tag and listed for the owner; commits after `v0.51.0` made before the completion plan carry a personal address ([completion plan](completion-plan.md#lead-decisions-owner-review-pending)). **Not done:** G1 on the final candidate (0.59.0 is not cut; the completion plan cuts final versions after the owner session). | The history and commit-identity decision (A, B or C); acceptance of the listed history findings; the legal review; Gpub. |
| F1 | Version, capabilities and mismatch diagnostics: the manifest reports package, contract, schema and config identity and the broker generation; an incompatible proxy, broker or configuration refuses clearly. | met | `v0.51.0`: schema-mismatch recovery through the installed SDK and Inspector. `v0.52.0`: guard refusals name `capability_disabled`, `config_stale` and `client_schema_mismatch` through the official SDK (`tests/test_guard_refusals.py`), and on the real install a session with an old proxy was refused `config_stale` ([0.52 task record](0.52-qr-onboarding-spec.tasks.md)). `tests/test_contract_handshake.py`, `tests/test_version_consistency.py`, `tests/test_server_guidance.py`; matrix cells `handshake.*` (five protocol revisions), `census.fingerprint`, `refusal.config_stale`, `refusal.client_schema_mismatch` on every candidate since `v0.54.0rc1`. | None. |
| F2 | Selected full messages and batch reads: bounded text or caption, sender, direction, timestamps, reply and topic metadata; per-anchor outcomes. | met | `v0.51.0` live reads through the installed SDK and Inspector; `tests/test_message_reader.py`, `tests/test_message_read_boundary.py`, `tests/test_reader_links.py` (validated `t.me` links, `v0.54.0rc1`); matrix call of `get_message_context`. | None. |
| F3 | Latest and date-bounded history: explicit scope, half-open intervals, continuation, honest edits, duplicates, exhausted budgets and unknown coverage. | met | `v0.51.0` live history; a real `read_history` on 0.52 through the official SDK ([0.52 task record](0.52-qr-onboarding-spec.tasks.md)); `tests/test_history_reader.py`, `tests/test_history_boundary.py`. The pinned provider cannot certify complete coverage, and the reader says so. | None. |
| F4 | Reply chains, then forum topics and topic history: bounded ancestry, cycle and cross-chat checks, current topic shapes, real forum evidence. | met | `v0.51.0`: topic paths through the installed SDK and Inspector, and a provider-confirmed forum send read back; `tests/test_reply_chain.py`, `tests/test_forum_reader.py`, `tests/test_forum_boundary.py`, `tests/test_forum_tdjson.py`, `tests/test_topic_history_reader.py`, `tests/test_topic_history_boundary.py`, `tests/test_topic_history_tdjson.py`. | None (the owner session's forum fixture re-confirms it on the final candidate). |
| F5 | Pagination beyond the first exact-search page: separate page and scope completeness, continuation, provider coverage. | met | `v0.51.0` live search continuation through the installed SDK and Inspector; real `search_correspondence` on 0.52 through the official SDK, `codex exec` and Claude Code; `tests/test_exact_search_reader.py`, `tests/test_exact_search_boundary.py`, `tests/test_exact_search_ipc.py`, `tests/test_search_service.py`. | None. |
| F6 | Sender, direction and topic filters; bounded Boolean search with all/any/none, rehydrated evidence, branch deduplication and lexical limits. | met | Accepted in `v0.51.0`; `tests/test_typed_search.py`, `tests/test_boolean_search_reader.py`, `tests/test_boolean_search_boundary.py`; the direction filter through the official SDK and real `codex exec` on synthetic data (`tests/test_conversation_scenarios.py`, 0.54). The public record names no consumer run of the Boolean form (open item 9). | None. |
| F7 | Chat listing and unread metadata, search over at most five selected chats, verified target reuse; no read receipts, no wildcard scope, fresh identity checks before actions. | met | Accepted in `v0.51.0`; `tests/test_chat_list_reader.py`, `tests/test_chat_list_boundary.py`, `tests/test_selected_search.py` (official SDK in process), `tests/test_verified_targets.py`, `tests/test_verified_target_boundary.py`, `tests/test_target_resolver.py`, `tests/test_target_discovery.py`; matrix cell `cursor.list_chats`; a real `resolve_target` on 0.52. Code check: the TDLib allowlist has no read-receipt request (`viewMessages` is absent), pinned by the exact-set test in `tests/test_caption_edit.py`; per-chat coverage is at most five rows (`SearchChatsResponse`). | None. |
| F8 | Continued attachment reading: selected pages and opaque text continuation bound to the artifact bytes and parameters; PDF limits, corruption and expiry. | met with owner remainder | `v0.51.0`: native Codex read a real text attachment through both readers; corrupt-image recovery through the installed SDK. Matrix cells `image.png` and `image.pdf_page`; `tests/test_attachment_pages.py`, `tests/test_attachment_page_boundary.py`, `tests/test_attachment_page_broker.py`, `tests/test_document_page_reader.py`, `tests/test_text_continuation_formats.py`, `tests/test_artifact_store.py`; the selection refusal of `read_attachment_page` fixed in 0.54 (`tests/test_next_actions.py`). | Installed file and page coverage on real artifacts ([0.54 owner row F](0.54-conversational-usability-spec.md)). |
| F9 | XLSX, then PPTX and the existing text formats: selected sheets and ranges or slides and notes; formulas are data; no macros or external resources; parser safety. | met with owner remainder | Accepted in `v0.51.0` (the real text attachment, F8); `tests/test_xlsx_reader.py`, `tests/test_spreadsheets.py`, `tests/test_spreadsheet_boundary.py`, `tests/test_spreadsheet_proxy.py`, `tests/test_pptx_reader.py`, `tests/test_presentations.py`, `tests/test_presentation_boundary.py`, `tests/test_presentation_proxy.py`, `tests/test_docx_safety.py`. The public record names no consumer run of the XLSX or PPTX readers (open item 9). | Installed file coverage on real XLSX and PPTX artifacts ([0.54 owner row F](0.54-conversational-usability-spec.md)). |
| F10 | Streaming local file transfer: the trusted helper selects paths; bounded memory and output; byte and hash equality; safe interrupted transfers. | met | Installed official SDK consumers in auto and legacy modes with synthetic files: ownership, cancellation, malformed input, lost-response recovery ([roadmap](../roadmap.md), B2C); `tests/test_transfer_cli.py`, `tests/test_transfer_download.py`, `tests/test_local_upload.py`, `tests/test_local_upload_authority.py`, `tests/test_uploaded_artifact_read.py`, `tests/test_artifact_ownership.py`; per-account refusal of a foreign download (`tests/test_account_isolation.py`, `v0.55.0rc1`). Local by design: no Telegram delivery is claimed. | None. |
| F11 | Draft list, get, update, cancel and refresh: client and account isolation, immutable revisions, finite expiry, fresh approval after changes, safe concurrent cancel and send. | met | `v0.51.0`: draft revision and cancellation through the installed SDK and Inspector. `tests/test_outgoing_drafts.py`, `tests/test_draft_revisions.py` (official SDK), `tests/test_draft_boundary.py`, `tests/test_draft_models.py`, `tests/test_draft_kinds.py`; every draft kind isolated across accounts (`tests/test_account_isolation.py`); one owner dialog at a time and one per draft (`tests/test_approval_serialization.py`, `v0.54.0rc1`, `v0.57.0rc1`). | None (real dialog clicks are owner rows under F13–F17). |
| F12 | Send status and uncertain-outcome reconciliation: real provider updates and temporary/final ids; evidence strength; never infer delivery or retry automatically. | met | `v0.51.0`: a provider-confirmed Saved Messages send read back at its exact anchor, also through the SDK; a historical unknown stays unknown. `tests/test_send_status.py`, `tests/test_broker_outgoing.py`, `tests/test_formatted_native_observations.py`; matrix cell `recovery.check_send_status` (one dispatch, no resend); the pending-send gate counts sends in flight, open dialogs and unsettled outcomes (`tests/test_pending_probe.py`, `v0.53.0rc2`). | None. |
| F13 | Replies, topic-aware sends, then formatted text: bind account, recipient, topic, reply, text, entities and options to the preview; revalidate before transport; validate UTF-16 spans and changed or deleted sources; real approved delivery for changed paths. | partly met | `v0.51.0`: provider-confirmed formatted and forum sends with readback; topic sends and text replies (0.49, 0.50). `v0.58.0rc1` ([spec](0.58-reply-sources-and-captions-spec.md), [task record](0.58-reply-sources-and-captions-spec.tasks.md)): media TextUrl, CustomEmoji and voice-note MediaTimestamp sources, styled captions and the consolidated ladder (`tests/test_reply_ladder_matrix.py`, `tests/test_reply_source_ladder.py`, `tests/test_reply_families_058.py`, `tests/test_formatted_captions_058.py`, `tests/test_caption_observations_058.py`, `tests/test_dialog_goldens_058.py`), technical only. Send paths changed since the real runs, technical only: Telegram's text rewrite before the preview (0b29806, `v0.52.0`), plain reply and topic confirmation with detected entities (057b, `v0.57.0rc1`), the caption read-back (`v0.58.0rc1`). **Agent-side gap:** consumer-matrix cells for the 0.58 tool changes (F18, in progress). | Real sources (a voice note captioned with a timestamp, custom emoji from a Premium sender or a boosted group, a media caption with a hidden link) and one approved styled-caption send; real approved delivery on the changed paths (any approved text send exercises the rewrite). |
| F14 | Forward existing messages: exact source and destination, explicit forward versus copy, protected-content checks, source revalidation, one transport attempt with a receipt. | met with owner remainder | `v0.52.0` ([spec](0.53-forward-copy-spec.md), [task record](0.53-forward-copy-spec.tasks.md)); `tests/test_forward_send.py`; a real approved copy and forward to Saved Messages, TDLib-confirmed and read back through the official SDK on a source install (20.10.2026). | A visual check of the forward header in a Telegram app. |
| F15 | Native video, then ordered albums: immutable media with validated metadata; ordered bounded album preview; per-item outcomes; no whole-album retry. | partly met | F15a, `v0.52.0` ([spec](0.53-native-video-spec.md)): `tests/test_video_probe.py`, `tests/test_video_send.py`; a real 3-second MP4 sent with its caption, confirmed and read back. F15b, `v0.56.0rc1` ([spec](0.56-albums-spec.md), [task record](0.56-albums-spec.tasks.md)): `tests/test_album_send.py`, `tests/test_album_surface.py` (official SDK in process), `tests/test_album_dialog.py`, `tests/test_outgoing_stage.py`, `tests/test_account_isolation.py`; technical only. **Agent-side gap:** consumer-matrix album cells (F18, in progress). | One real album sent and read back with its dialog approved; how Telegram clients show several captions. |
| F16 | Multiple accounts: trusted local aliases with separate sessions, sockets, locks and caches; every handle and receipt bound to its account; validation with two actually authorized accounts. | met with owner remainder | `v0.55.0rc1` ([spec](0.55-multiple-accounts-spec.md), [task record](0.55-multiple-accounts-spec.tasks.md)): the default alias byte-identical to 0.54 (`tests/test_account_paths.py`, `tests/test_account_default_goldens.py`); AST and runtime sweeps (`tests/test_account_paths_sweep.py`); registry and pin (`tests/test_account_registry.py`, `tests/test_account_pin.py`); the isolation matrix on two real brokers over fake TDLib sessions for every handle kind (`tests/test_account_isolation.py`); installer and kill sweep (`tests/test_account_install.py`, `tests/test_account_changes.py`, `tests/test_bundle_kill_sweep.py`); one dialog across brokers (`tests/test_approval_serialization.py`). Technical only; the consumer matrix has no alias session, so consumer evidence for an alias comes with the owner rows. | A second account added by QR; one approved action on each account; the dialog names the right one (installed bundle, real client). |
| F17 | Archive and unarchive, edit, delete, pin and unpin: independent default-off capabilities; before and after preview with side effects; exact approval; one apply; provider-state verification; no mass moderation. | partly met | F17a, `v0.52.0` ([spec](0.54-archive-and-edit-spec.md), [task record](0.54-archive-and-edit-spec.tasks.md)): `tests/test_archive_edit.py`; a real own-text edit applied and read back; a supergroup archived and unarchived with the list observed; Telegram's refusal surfaced as `failed`, not retried. F17 rest, `v0.57.0rc1` ([spec](0.57-own-message-actions-spec.md), [task record](0.57-own-message-actions-spec.tasks.md)): `tests/test_caption_edit.py`, `tests/test_caption_surface.py`, `tests/test_styled_text_edit.py`, `tests/test_pin_change.py`, `tests/test_message_delete.py`, `tests/test_message_action_capabilities.py`, `tests/test_message_action_dialogs.py`, `tests/test_provider_entity_forms.py`; fakes modelled on the pinned TDLib source; technical only. Code check: bulk, history, by-sender and by-date deletes and `unpinAllChatMessages` are absent from the allowlist; `deleteMessages` passes only its own transport. **Agent-side gap:** consumer-matrix cells for the 0.57 tools (F18, in progress). | Real caption edit, styled text edit, pin, unpin and deletion, each approved in the dialog; a deletion in a private chat and in a group; the Saved Messages and other-people refusals on a real account; the server's answer for an already self-deleted message. |
| F18 | Consumer matrix (Codex with the skill, Inspector without, an independent official SDK consumer: schemas, protocol, errors, images, cursors, recovery, limits, approval denial), installation, and controlled TDLib upgrades with their own ABI and provider checks. | partly met | Synthetic matrix: 32/32 cells with a verified receipt on every candidate since `v0.54.0rc1` ([matrix spec](0.54-consumer-matrix-spec.md)). **In progress, evidence pending:** its extension to every tool through 0.58 and the Inspector, Codex and Claude Code agent cells (another branch; filled after that merge). Real `codex exec` conversation scenarios on synthetic data, 8 of 8 (0.54, 9eb8d8e); `claude -p` was not logged in. Installation, upgrade, rollback, uninstall and adoption: technical (P2). TDLib: the [procedure](../tdlib-upgrade.md) and the [rehearsal](final-tdlib-rehearsal.md), three clean rebuilds of d1085f9 byte-identical to each other and the shipped pin reproduced from the keg; criterion 3 met in one reading (`tests/test_build_tdlib_source.py`, `tests/test_prepare_native.py`, `tests/test_tdlib_media_schema.py`). Real: 0.52 calls through the official SDK, Codex and Claude Code on the owner's account; 0.51.0 Inspector and native Codex. | The real-account cells (SDK, Inspector, Codex, Claude Code), Claude Desktop and the plugin, real dialog clicks; the clean-user install; a real upgrade and rollback of an installed bundle (the migration); confirming the criterion 3 reading; step 7 of the TDLib procedure when a trigger comes. |
| F19 | Streamable HTTP only for a demonstrated cloud-consumer need (then default-off and fully guarded); the final audit; the final artifact and publication gates repeated. | partly met | HTTP: **not applicable**, no cloud consumer is demonstrated; STDIO-only transport, the Unix-socket broker and the loopback-only connect screen are pinned by `tests/test_transport_scope.py` (823ccf7, in every candidate since `v0.54.0rc1`); code check: the server runs `transport="stdio"` only. Final audit: this document; its critic is pending. Final gates: G1 on the final candidate not run; Gpub not given. | Gpub; the owner session that precedes the final G1. |

## Shared limits

Checked against the code at this branch; each is held by the tests named.

- **Untrusted content, no execution:** readers render Telegram text as data; formulas are data, no macros or
  external resources (`tests/test_xlsx_reader.py`, `tests/test_pptx_reader.py`, `tests/test_docx_safety.py`).
- **Sessions preserved, one broker per session:** `tests/test_session_owner.py`, `tests/test_onboarding.py`; per
  alias since 0.55 (`tests/test_account_install.py`).
- **Capabilities from trusted policy, never tool arguments:** `tests/test_capability_profiles.py`,
  `tests/test_guard_refusals.py`; the default stays `read` + `artifacts` (owner, 20.10.2026).
- **No read receipts; writes need approval:** no read-receipt request is allowlisted; every send and apply goes
  through the owner dialog (`tests/test_approval_prompt.py`, `tests/test_approval_serialization.py`).
- **Bounded pages, opaque bound cursors:** matrix cells `cursor.list_chats`, `recovery.restart_listing`,
  `caps.input_bounds`; `tests/test_account_isolation.py` for cursors across accounts.
- **Immutable hash-bound payloads; no automatic retry of an unknown send:** draft digests and one-attempt
  transports (`tests/test_outgoing_drafts.py`, `tests/test_album_send.py`, `tests/test_message_delete.py`).
- **Never reachable:** secret chats (`use_secret_chats=False`), transcript export, a private search index, a raw
  TDLib bridge (closed allowlist, `_call` refuses every state change), clear-history and delete-chat requests,
  mass moderation (exact-set tests in `tests/test_caption_edit.py`).
- **Platform:** macOS ARM64 with the pinned TDLib only; the TDLib procedure keeps that pin until a trigger.

## Release gates

**G1 per candidate.** Every row below passed `release_gate.py run` and `verify` on its tagged commit with the full
suite in the archive tree; the evidence and receipts are kept outside the public tree, as
[releasing](../releasing.md) requires.

| Tag | Commit | Suite (passed, skipped) | Bundle stage | Privacy hits | History findings | Consumer matrix |
| --- | --- | --- | --- | --- | --- | --- |
| `v0.52.0` | 40ebf57 | 2692, 0 | — | 0 | reported for the history decision | — |
| `v0.53.0rc1` | 05fee70 | 3116, 0 | ad hoc dry run; never ran the seed, which refused its own version | 0 | not recorded | — |
| `v0.53.0rc2` | 482422b | 3138, 0 | the kit's own seed verifies the kit (prepare-only) | 0 | not recorded | — |
| `v0.54.0rc1` | c6338a0 | 3347, 0 | yes | 0 | 1 (cfe5dc3) | 32/32, verified |
| `v0.55.0rc1` | 1c8b9f4 | 3781, 0 | yes | 0 | 1 (7613259) | 32/32, verified |
| `v0.56.0rc1` | 6c8070e | 3896, 0 | yes | 0 | 2 (022e18f, 4a3f926) | 32/32, verified |
| `v0.57.0rc1` | 1c41b5a | 4106, 0 | yes | 0 | 1 (84b8421) | 32/32, verified |
| `v0.58.0rc1` | 6d1680d | 4189, 0 | yes | 0 | 0 | 32/32, verified |

Every itemized history finding (0.54–0.57) is a synthetic false positive replaced at the tip; the 0.52.0 report is
not itemized in the public record. Each is listed for the owner's acceptance in the [decision sheet](owner-decision-sheet.md#4-history-findings-for-acceptance-at-gpub). The roadmap's
0.53.0rc2 paragraph still says rc2 has no tag or gate evidence; the tag and its gate exist (open item 8).

**G1 for the final candidate.** Not run. It follows the F18 merge, this audit's critic and the owner session, whose
defects would force a rebuild; it re-runs the gate with the bundle stage, the consumer matrix with every cell, and
the applicable real consumer and provider scenarios on that exact commit (original plan, G1).

**Gpub.** Not given. Nothing is pushed to `main` and no tag has left the machine. Gpub needs the owner's history and
identity decision, acceptance of the listed history findings (or the chosen remediation and a new gate run), the
product name and API identity if the release carries assets, the legal review, and the remote pre- and post-checks
of [releasing](../releasing.md), section 8.

## Open items

1. **Default first-install version line** (recorded 24.10.2026 from 0.55, [task record](final-acceptance-spec.tasks.md)).
   A first install rewrites the default's `runtime.toml` version line only when it adopts a legacy service
   (`--adopt-legacy`; checked in `bundle_install.install`). After an uninstall and a reinstall at another version
   the default broker meets `config_stale`, and the next upgrade `policy_conflict`. 0.53/0.54 behaviour, kept
   unchanged by 0.55 under its criterion 2. Needs a decision: fix it in the installer (the auth/session lane, so
   sequential) or disclose it as a known limit in the final notes.
2. **F18 consumer matrix extension and agent cells** (in progress on another branch, evidence pending). When the cells
   for the 0.56–0.58 tools pass, F13, F15 and F17 move to met with owner remainder; F18 itself stays partly met until
   its agent cells pass and then carries only owner rows.
3. **Criterion 5 critic.** An independent critic reviews this table against the code; its findings are fixed or
   recorded before criterion 5 is ticked.
4. **Mounted-DMG and `Install.command --prepare-only` run** of the ad hoc dry-run kit on the final candidate
   (open since 0.53.0.dev0; [rc2 record](0.53-client-distribution-spec.tasks.md#release-candidate-0530rc2-22102026)).
5. **Final G1, release notes and roadmap** for the final candidate (spec work item "Release notes and roadmap").
6. **`checkDatabaseEncryptionKey`.** Absent from the pinned `td_api.tl` (its state is not in the schema, so it is never
   sent) but still allowlisted and called (`tdjson.py`, `auth_transport.py`). Removal is a separate reviewed
   allowlist diff, on the decision sheet.
7. **Recorded lows still open:** 0.53 (`tools/list` still lists every enabled tool after a schema mismatch; the
   instructions' recovery line names the setup skill, absent in Claude Desktop, and has no `config_stale` step; a v1
   native lock bypasses the approved-pin table; `repin_native.py` does not check that the signed natives re-sign its
   unsigned inputs; git replace refs could change exported files; the eight-write bound per skill copy; a broker a
   concurrent `connect` loads at uninstall's stop; a plist removed after a commit with no broker running is not
   written again), and 0.54 (answer-wording heuristics use word lists; the matrix does not drive the plural-profile
   or base-capability refusal texts, does not assert that `prepare_forward_send` is unlisted, and checks only the
   over-cap side of each bound; the runner watches only shell tools; the profile switch's temporary after a kill,
   no installer lease around a switch, a FIFO at `runtime.toml`, a send started between the gate and the restart).
8. **Stale public statements** for the final roadmap and notes pass: the roadmap's 0.53.0rc2 paragraph (no tag or gate
   evidence; both exist), the 0.58.0rc1 changelog entry ("the independent critic … pending"; criterion 7 is met), and
   the status line of the [0.52 QR requirement](0.52-qr-onboarding.md) ("real acceptance (phase F) pending"; only
   the clean-user bullets are).
9. **Public-record gap for 0.51.0 forms.** F1–F12 rest on the 0.51.0 acceptance, which the public notes name only in
   summary. The Boolean search form (F6) and the XLSX and PPTX readers (F9) have only technical evidence in the
   public tree. The final G1's fresh checks should call each once through an installed consumer (or the owner
   session through a real client).
10. **Changed send paths without a real send:** Telegram's text rewrite before the preview (0b29806), the plain reply
    and topic confirmation with detected entities (057b) and the caption read-back (0.58). The owner session's
    approved sends cover them; until then F13 says so.
11. **Accepted gaps, disclosed:** a dialog already open when the first `account-add` runs holds only its own broker's
    lock (0.55, A12); a deletion reads `applied` after TDLib's local read, so a deletion made for yourself elsewhere
    that this session has not received can read `applied` after a server-side no-op (0.57); album captions stay
    plain and audio albums are deferred (0.56); the shipped natives come from the Homebrew keg, not from the source
    rebuild (TDLib rehearsal).
