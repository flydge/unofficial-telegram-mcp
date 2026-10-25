# Owner decision sheet

**Status:** lead record, 25.10.2026, for the owner session at the end of final acceptance. It consolidates the
[completion plan](completion-plan.md#owner-decision-sheet-prepared-for-the-end)'s decision list, every decision the
specs and task records mark **lead decision, owner review pending**, the decisions recorded since 0.55 that need the
owner's confirmation, the history findings held for Gpub and every release's owner rows as one ordered check list.
Each line links to its source, which holds the reasoning and the rejected options. The requirement status is in the
[final audit](final-audit.md).

Nothing here is decided by this sheet. Where a line gives a lead recommendation, the owner may take another option;
a change to a behaviour the owner session exercises means a rebuild before that evidence.

## 1. Decisions the owner takes

From the completion plan's numbered list, plus two items the specs hand to the owner.

| # | Decision | Options and what depends on it | Source |
| --- | --- | --- | --- |
| 1 | Review the lead decisions of sections 2 and 3, including the release order (0.52, 0.53, 0.54, then F16, F15b, F17, F13 in the owner's stated order, then final acceptance) | Accept, or name the ones to change | [completion plan](completion-plan.md#release-sequence) |
| 2 | Public history and commit identity | **A** accept as is; **B** rewrite the unmerged range to the public identity with redactions and force-push the branch before `main` moves; **C** rewrite `main` (not recommended). Must be decided before `main` moves | [completion plan](completion-plan.md#lead-decisions-owner-review-pending), [releasing](../releasing.md) section 5 |
| 3 | Product name, and whether the MCP server name, the plugin name (placeholder `telegram-local`, server `tg`) and the skill ids change with it | One rename commit through `product_identity.py`, before the signed build and before any public listing (another vendor's listing already uses the current name) | [0.53 spec](0.53-client-distribution-spec.md), phase A and G |
| 4 | API identity of the public bundle | The current pair or a dedicated application pair in its own Keychain service; the release build ships no identity by default. A change refuses existing enrollments, so it is decided **before any end-session evidence** | [owner checklist](0.53-signing-owner-checklist.md#4-application-identity-in-the-bundle-clients-16) |
| 5 | Signing and notarization | Identity and notary profile provisioned on 21.10.2026 (preflight ok); the signed build, the release commit and the observations remain | [owner checklist](0.53-signing-owner-checklist.md#5-the-signed-release-build) |
| 6 | Legal | Telegram API terms (including the AI-use clause recorded as a risk in 0.52), the third-party notice set, the AGPL corresponding-source claim | [0.52 spec](0.52-qr-onboarding-spec.md#8-risks-and-degradation), [licensing](../licensing.md) |
| 7 | Real fixtures on a test account | A small group; a forum supergroup with an open topic; captions with links; a Premium sender or a boosted group for custom emoji; a voice note of at least three seconds captioned with a timestamp | [completion plan](completion-plan.md#owner-decision-sheet-prepared-for-the-end), [0.58 task record](0.58-reply-sources-and-captions-spec.tasks.md) |
| 8 | Clean macOS user run with the signed build | Section 5, phase C | [0.52 requirement](0.52-qr-onboarding.md#acceptance-and-release-gate) |
| 9 | One real-evidence session | Section 5, phase D | [completion plan](completion-plan.md#owner-decision-sheet-prepared-for-the-end) |
| 10 | F16 two-account validation | Section 5, phase E | [0.55 spec](0.55-multiple-accounts-spec.md) |
| 11 | Migration of the owner's daily install, including the Claude Desktop and Codex entries the installer does not own | `--adopt-legacy` from the signed kit; section 5, phase F | [0.53 spec](0.53-client-distribution-spec.md) |
| 12 | Gpub | An explicit authorization naming the refs and artifacts to publish | [releasing](../releasing.md) section 8 |
| 13 | Final acceptance criterion 3 (TDLib procedure) | See section 2 | [rehearsal record](final-tdlib-rehearsal.md#criterion-3-what-is-shown-and-the-reading-the-owner-confirms) |
| 14 | Localisation of the approval dialog | Labels stay English until the owner decides | [0.54 spec](0.54-conversational-usability-spec.md), item C |
| 15 | The 0.52 QR gate amendment | Owner decision of 20.10.2026: the clean-user run moved to the backlog and 0.52.0 cut with every unmet bullet disclosed; the requirement puts it on this sheet for confirmation | [0.52 requirement](0.52-qr-onboarding.md#acceptance-and-release-gate) |

## 2. Decisions recorded since 0.55 for the owner to confirm

- **The final candidate is named 0.59.0.** Lead decision recorded with the development bump (b8d384b); the owner may
  name the published version at Gpub, for example 1.0.0; another version means a version bump at every version
  site and a new gate run before the tag.
- **The reading of criterion 3.** Met in this reading: independent clean rebuilds of d1085f9 from source are
  byte-identical to each other; the shipped, Homebrew-derived natives and lock are reproduced by `prepare_native.py`
  from the installed keg, not by a rebuild. If the criterion means rebuilt natives equal to the shipped ones, it is
  not met, and meeting it is a change of the shipped build method
  ([rehearsal](final-tdlib-rehearsal.md#criterion-3-what-is-shown-and-the-reading-the-owner-confirms)).
- **`checkDatabaseEncryptionKey` in the request allowlist.** It is absent from the pinned schema (its authorization
  state does not exist there, so it is never sent), yet still allowlisted and called (`tdjson.py`,
  `auth_transport.py`). The rehearsal left its removal to a separate reviewed allowlist diff. Options: remove it now
  in such a diff on the auth path (both call sites and the allowlist, with tests), or at the next pin change, whose
  step 3 diffs the allowlist anyway ([rehearsal](final-tdlib-rehearsal.md#steps-3-and-4-on-the-pin)).
- **The source rebuild as the shipped build method.** Not proposed: it is a reviewed v1 grammar change (the build
  method literal), and the rebuilt libtdjson differs from the shipped one in toolchain and flags. Options: keep the
  Homebrew-derived natives (lead recommendation), or adopt the rebuild in a separate spec
  ([rehearsal](final-tdlib-rehearsal.md#what-this-does-not-show)).
- **No installer guard for a TDLib session-database migration now.** The guard ships in the launcher of the first
  release that changes the pin; that upgrade's spec decides it. Until then the documented owner backup and restore
  covers it ([rehearsal review fixes](final-tdlib-rehearsal.md#review-fixes-25102026), item 1).
- **No `read` for the new reply families** (custom emoji, voice-note timestamps, media TextUrl), although the
  general rule says that revealing content needs `read`: a reply source is always a message the client already read
  ([0.58 spec](0.58-reply-sources-and-captions-spec.md#4-decisions-and-rejected-options)).
- **The default first-install version line** (final audit, open item 1): fix the installer in the final candidate
  (the auth/session lane, so built sequentially and before the owner session's build), or disclose it as a known
  limit of 0.59.0.
- **The history findings** of section 4, to accept or to remediate before Gpub.

## 3. Lead decisions, owner review pending, by release

One line per decision group; the source has the detail and the rejected options.

### Completion plan (20.10.2026)

- Commit identity: new commits use the public lineage identity through a repo-local config; earlier commits are
  decision 2 ([plan](completion-plan.md#lead-decisions-owner-review-pending)).
- Publication hold: `main` is not moved on the remote and no tag is pushed until the history decision; working
  branches are pushed as backups.
- Evidence chain for 0.52.0: each real row carried to the tag with its diff classified; sends before the text
  rewrite disclosed.
- Hash fixture scope (schemas and annotations; descriptions guarded by a budget test) and the tool census defined on
  the full registration.
- Every new action its own default-off capability; state changes need `send`, content-revealing preparation also
  `read`.
- TDLib stays on d1085f9 / 1.8.67 without a trigger; F19 HTTP not applicable, guarded by a test.
- Network: only pinned, hash-verified inputs; no remote write but backup branches before Gpub.
- The release sequence, local release-candidate tags and one owner session against the final candidate.

### 0.52 (not marked pending; no owner review of them is recorded)

- The spec amendments of 16.10.2026 from the integration review: refusal codes, probe timings, `retry_after`, the one
  `next_action` table, the `cancel` command, expiry by the wall clock ([spec](0.52-qr-onboarding-spec.md#8-risks-and-degradation)).
- Client config roots need only owner ownership and no group or world write, since the clients create them 0755
  ([task record](0.52-qr-onboarding-spec.tasks.md), phase E).
- A pre-0.52 enrollment is used without fingerprint verification until a state write binds it (phase A).
- Already reviewed: the F14, F15a and F17a decisions, accepted by the owner on 20.10.2026
  ([owner review](0.54-archive-and-edit-spec.md#owner-review-20102026)).

### 0.53 client distribution ([spec](0.53-client-distribution-spec.md#6-decisions-and-rejected-options), [task record](0.53-client-distribution-spec.tasks.md))

- Container: a DMG with `Install.command` (a flat package and a zip rejected).
- Trust root: the sha256 and index pins plus a Team ID check (Team ID alone rejected).
- Quarantine kept on the staged seed.
- Exact release paths for Codex, Claude Code and Claude Desktop; only the plugin uses the `current.json` shim.
- A conflicting Claude Desktop entry refuses the install; adoption is explicit (`--adopt-legacy`).
- Shell-less onboarding: inline `connect` in `Install.command` plus server instructions (no setup tools).
- Plugin and user scope are exclusive (`--claude-code`).
- The marketplace lives in the product repository; the official directory only after the rename.
- Signing runs locally on the owner's Mac (CI with an exported identity rejected).
- One `release-pins.json` schema and one trust root, the committed copy at the release tag; `Install.command` not
  signed for now (its Gatekeeper behaviour is an owner observation first).
- A refusal after the broker stop restores the service, not the clients; later rounds: the service step and the
  restore idempotent by state, no cycle limit, a parked change ends by its repeat or `uninstall`, killed client
  writes' temporaries removed.
- Accepted, documented: a Claude Code or Claude Desktop entry the user changed while an interrupted step wrote it
  stays the user's.
- Round 6: one version grammar at every site; a skill copy touched after the receipt is published is the user's;
  a plist removed by hand is written anew; a parked change owns an entry by its write-ahead record; the pending-send
  gate counts sends in flight, open dialogs and unsettled outcomes; the eight-write bound per skill copy stays
  ([rc2 record](0.53-client-distribution-spec.tasks.md#release-candidate-0530rc2-22102026)).
- A parked adopting first install has no uninstall path; its repeat ends it (round 4).
- MCPB desktop extension rejected; standalone Python pruned; policy-filtered `tools/list` with the full-registration
  fingerprint unchanged.
- The release build ships no application identity by default; a shipped one should be a dedicated pair
  ([owner checklist](0.53-signing-owner-checklist.md#4-application-identity-in-the-bundle-clients-16)).

### 0.54 conversational usability ([spec](0.54-conversational-usability-spec.md#5-decisions-and-rejected-options), [task record](0.54-conversational-usability-spec.tasks.md), [matrix spec](0.54-consumer-matrix-spec.md#8-decisions-and-rejected-options))

- The approval dialog is a JXA `NSAlert`; the `display dialog` fallback was never shipped.
- The skill stays a single file; hashes leave the chat preview; no `not_attempted` status.
- The dialog summary names the chat by its numeric id and shows the chat type only where the bound draft carries it.
- Annotations: read-only readers, prepares, gets and lists; `destructiveHint` only on overwriting or deleting
  applies; the title is the top-level MCP `title`.
- `approved=false` returns `declined` without a dialog; a capability disabled at send time keeps `expired`.
- A refresh asks only for what the newly read source needs (the old and new rule retired), with the documented
  `update_reply_draft` exception.
- The closed `next_action` values and the four prompts.
- Schema-change bookkeeping: one fixture per slice and a frozen structure-only 0.52 fixture.
- `client_schema_mismatch` clears only through `_manifest` with the current fingerprint; no `check_connection` hint
  invented for core tools.
- The capability-profile switch refuses while any client context is active and is never a mid-session step; a
  rollback accepts a policy that differs only in its `enabled_capabilities` line.
- One owner dialog at a time: a send meanwhile answers `dialog_unavailable`, never queued.
- The conversation rubrics decide on the server's transcript of actions; the wording of the final answer is advisory.
- Consumer matrix: the pinned closure, the running interpreter's build tools, one HOME per session in parallel, an
  unknown outcome from a transport lost after dispatch, a `ClientSession` per revision, hard-coded expected texts, the
  real dialog opt-in, agent cells outside the harness, frozen counts, `check` reinstalls, the gate bound like the
  harness.

### 0.55 multiple accounts ([spec](0.55-multiple-accounts-spec.md#6-decisions-and-rejected-options), [task record](0.55-multiple-accounts-spec.tasks.md))

- Sibling roots for aliases, one Keychain service, no handshake or schema change, the alias in argv only, the plugin
  default-only, removal moves aside.
- Amendments A1–A11: byte-identity until the first `account-add` and the registry's lifetime, the alias reaching the
  model, the socket budget at every admission, the pin before every operation outside a closed exempt set, code
  families, the dialog's account line, removal order, retirements, the repair path's version line, eight draft
  kinds, a pending first install ([amendments](0.55-multiple-accounts-spec.md#amendments-24102026)).
- Amendments A12–A16 from the critic: one dialog across brokers, the instructions line naming `--account`, login
  locks in installer refusals (`connect_lock_unsafe`), `default_check: unanswered`, `older_state_kept`
  ([critic amendments](0.55-multiple-accounts-spec.md#amendments-24102026-critic-fixes-055f)).
- Phase C: the masked account is the last two digits; the login check pins; `attempt_outcome` keeps the 0.54 rule;
  budget routing reads the last observed account; an alias's status omits the default's clients.
- Phase D: registry and sidecar roles, ownership by exact equality, late refusals resume, a `blocked` default check
  proceeds, the plist unlinked and the root moved, a foreign lock holder refuses, the sidecar never outlives the
  last alias.
- Phase D part 2: a change goes over registry and sidecar aliases together; alias entries owned while exactly one
  of this installation's releases; the sidecar republished before the commit; the default restored first; the
  registry retired below 0.55; uninstall keeps alias sessions; an alias added after an upgrade blocks its rollback.
- Review 055d: an alias listed without a record is repairable by its own action; `release_not_current`; an idle
  connect job is no login; a plist a login wrote during a change is rewritten after the commit; named aliases only.
- Review 055d2: lead decision A (every installer transaction holds every alias's connect lock), two-phase lock
  creation, account-add leaving its own alias out, `owner_busy` for `setup`/`resume`, result outcomes not codes, an
  alias whose state changed stays on its release (`newer_state_kept`).
- Review 055d3, R1–R4: only account-add creates an alias root; `transaction_pending` for logins; the newer plist set
  aside; the live-socket probe only when the interpreter is gone.
- After 055d4: the repair moves an alias's version line only from an equal or older release (`policy_conflict`
  otherwise).
- Critic 055f: the dialog lock created only inside the owner-only folder and never by account-add; an own code
  for an unsafe connect lock, never repaired; `default_check_failed` removed; the login code names.
- Accepted gap: a dialog already open when the first `account-add` runs holds only its own broker's lock.

### 0.56 albums ([spec](0.56-albums-spec.md#5-decisions-and-rejected-options), [task record](0.56-albums-spec.tasks.md))

- A separate album status model; audio deferred; no album retry; one staging directory per item.
- Part 1: its own `albums` profile depending on `send` only; video items also need `video_send`; the request names
  the group (`media` or `documents`); three album-level refusal codes; derived staging names; the observation and
  status-evidence rules; a 120-second wait; the dialog seam
  ([part 1](0.56-albums-spec.md#part-1-core-lead-decisions-owner-review-pending)).
- Part 2: `recipient` as the argument name; a structured `capability_disabled` for the video conjunction;
  `partial` with unconfirmed items carries `check_send_status`; the dialog layout; one dialog only; the budget;
  proxy deadlines; the manifest's forbidden list ([part 2](0.56-albums-spec.md#part-2-surface-lead-decisions-owner-review-pending)).
- Review fixes: an optional per-item `display_name` with `display_name_required`; slots bind by chat and temporary
  id; retirement under the staging lock ([review fixes](0.56-albums-spec.md#review-fixes-056-lead-decisions)).

### 0.57 own-message actions ([spec](0.57-own-message-actions-spec.md), [task record](0.57-own-message-actions-spec.tasks.md))

- Deletion for everyone only in v1 (deletion for yourself refused until the owner asks); pins silent and never
  for yourself only; removing a caption allowed in one pinned form; styled text extends `prepare_text_edit`.
- Part 1: the draft-kind table; the two edits in `archive_edit`, new `pin` and `delete` profiles (an owner who had
  `archive_edit` on sees it partial after the upgrade until enabling it again); caption input refused, never
  rewritten; the verification rule
  ([part 1](0.57-own-message-actions-spec.md#part-1-draft-kind-table-capabilities-caption-edit--lead-decisions-owner-review-pending)).
- Part 2: styled text edit and its refusal of rewritten input; plain edits byte-identical; pin evidence is the F14
  projection; already-in-target statuses; the 4xx read-back for pins
  ([part 2](0.57-own-message-actions-spec.md#part-2-styled-text-edit-pin-and-unpin--lead-decisions-owner-review-pending)).
- Part 3: the scope and refusal order; the F14 projection shown before a deletion; its own transport; the read
  `ok` needs; the evidence order; restart replay disclosed; the `Delete` button (a typed confirmation rejected for
  now); recorded, not changed: the deletion window
  ([part 3](0.57-own-message-actions-spec.md#part-3-deletion-of-one-own-message-dialog-goldens-isolation--lead-decisions-owner-review-pending)).
- Review fixes 057: join TDLib's split pieces rather than port its cut points; the formatted send takes the same rule;
  media timestamps tolerated exactly; one dialog per draft through a reservation
  ([057](0.57-own-message-actions-spec.md#review-fixes-057--lead-decisions-owner-review-pending)).
- Review fixes 057b: bounds per kind; plain replies and topic texts take the shared rule; a whitespace-only span is
  kept; the skill names re-sending a download; what `applied` means for a deletion
  ([057b](0.57-own-message-actions-spec.md#review-fixes-057b--lead-decisions-owner-review-pending)).

### 0.58 reply sources and captions ([spec](0.58-reply-sources-and-captions-spec.md#4-decisions-and-rejected-options), [task record](0.58-reply-sources-and-captions-spec.tasks.md))

- No `read` for the new families (section 2); MediaTimestamp only on voice notes; the ladder consolidated first.
- Part 1: refusals not frozen by message; the stored-version check; datetime levels as rows; exclusivity and inert
  display as fields; the media display fails closed; the preview digest is the text reply's only
  ([part 1](0.58-reply-sources-and-captions-spec.tasks.md#part-1--freeze-and-ladder-table-work-items-1-and-2)).
- Part 2: both capabilities in `replies`; the schema fingerprint moves; criterion 4 read with work item 4; a
  timestamp must match its token; the duration bound as a parser argument; one emoji as a closed grammar; custom
  emoji not inert; the inert media record reports `sanitized`; budgets
  ([part 2](0.58-reply-sources-and-captions-spec.tasks.md#part-2--new-families-on-the-ladder-table-work-items-36-and-9)).
- Part 3: a styled caption must already be its stored form; video captions in scope; version 2 only for a styled
  caption; the inner draft keeps its digest; a plain send's read-back checks the caption; each tool keeps its closed
  refusal status; the dialog takes the stored projection; a styled caption's receipt disclosed like formatted text's
  ([part 3](0.58-reply-sources-and-captions-spec.tasks.md#part-3--formatted-captions-dialog-surface-work-items-7-and-8)).
- Review fixes: a refused styled-video replay answers `outcome_unknown`; a run killed by a signal still leaves its
  empty test HOME ([review fixes](0.58-reply-sources-and-captions-spec.tasks.md#review-fixes-058)).

### Final acceptance ([spec](final-acceptance-spec.md#2-work-items), [rehearsal](final-tdlib-rehearsal.md))

- The matrix columns and rows, the eight-step TDLib procedure, F19 not applicable, the audit with a critic.
- The 0.59.0 name, the criterion 3 reading, `checkDatabaseEncryptionKey`, the build method and the installer guard
  (section 2).
- An OpenSSL-only candidate builds row 0; a build timeout stays `build_failed` while Ctrl-C, SIGTERM and SIGHUP
  refuse `build_interrupted`; a signal inherited as ignored stays ignored.

## 4. History findings for acceptance at Gpub

The gate reports these; it never refuses on them. Each is accepted, or remediated by the history decision and a new
gate run, before Gpub ([releasing](../releasing.md) section 8). The final G1's report covers the whole unpublished
range and is reviewed again then.

| Candidate | Commit | File | Rule | What it is | Replaced by |
| --- | --- | --- | --- | --- | --- |
| 0.52.0 | range since `v0.51.0` | — | report and identities | history findings reported for the history decision (not itemized in the public record); commits before the completion plan carry a personal address (decision 2) | — |
| 0.54.0rc1 | cfe5dc3 | a test file | chat id | one synthetic test chat id in an intermediate commit | the tip |
| 0.55.0rc1 | 7613259 | `tests/test_account_paths_sweep.py` | address | two stacked decorator lines matched the address rule (a synthetic false positive) | 6150cbc |
| 0.56.0rc1 | 022e18f | `tests/test_album_send.py` | chat id | the canonical documentation example of a supergroup id | the tip |
| 0.56.0rc1 | 4a3f926 | `docs/plans/0.55-multiple-accounts-spec.tasks.md` | address | two stacked decorator names quoted in a note | 41b4177 |
| 0.57.0rc1 | 84b8421 | `docs/plans/0.56-albums-spec.tasks.md` | chat id | an evidence note quoting the documentation example of a supergroup id | 7afa54e |
| 0.58.0rc1 | — | — | — | none | — |

Sources: the release-candidate lines of the [0.54](0.54-conversational-usability-spec.tasks.md),
[0.55](0.55-multiple-accounts-spec.tasks.md), [0.56](0.56-albums-spec.tasks.md),
[0.57](0.57-own-message-actions-spec.tasks.md) and [0.58](0.58-reply-sources-and-captions-spec.tasks.md) task
records and the [0.52 task record](0.52-qr-onboarding-spec.tasks.md).

## 5. Owner-session check list

Every release's owner rows in one order. Record each with the date, the commit, the client and its version and what
was seen; a real send names its readback, and an unknown outcome stays unknown. Step 7 of the TDLib procedure runs
only when a pin change has a trigger, so it is not part of this session.

**A. Before any evidence**

1. Decide the API identity of the public bundle (decision 4).
2. Decide the product name; if it changes, the lead makes the one rename commit before the build (decision 3).
3. Confirm or change the lead decisions of sections 2 and 3 that the session exercises; a change means a rebuild
   first (decision 1).
4. Prepare the real fixtures on the test account (decision 7).

**B. Build, on the build Mac** ([owner checklist](0.53-signing-owner-checklist.md#5-the-signed-release-build))

5. The preflight again, then the ad hoc dry run and `check` of the final candidate, with a mounted-DMG
   `Install.command --prepare-only` run (final audit, open item 4).
6. The signed build and `check`, including the entitlement matrix with the Developer ID identity; the release
   commit with `release-pins.json` ([section 6](0.53-signing-owner-checklist.md#6-the-release-commit-plugin-pins)).
7. Observation 1: the DMG's Team ID, stapler and Gatekeeper assessment
   ([section 7](0.53-signing-owner-checklist.md#7-owner-session-observations)).

**C. Clean macOS user, from a browser-downloaded DMG, without developer tools** (0.52 gate, 0.53 phase G)

8. Double-click `Install.command` and record what Gatekeeper does; whether the staged seed carries quarantine and
   whether a first run without network is refused; that the installed release tree carries no quarantine
   (observations 2–4).
9. QR login on an account with 2FA; an observed QR refresh; page Cancel; screen expiry; kill the helper, then
   `connect` again.
10. Restart Claude Desktop; bounded searches in Claude Code, Codex and Claude Desktop; the plugin path
    (`--claude-code plugin`); the broker under launchd and the connect helper in this GUI domain.
11. Logout and login (or a reboot) and a repeat install, with no new login; a revoke from the phone, then `connect`
    reconnects.
12. No QR link, password or session key in chat, tool output or logs; the owner's own install untouched.

**D. Real-evidence session on the test account, each change approved in the dialog**

13. The redesigned dialog: a real Send click, a real Cancel click, a timeout, and a second send while a dialog is
    open answering `dialog_unavailable` (0.54 row F).
14. An approved text send (Telegram's rewrite), a plain reply and a topic send holding a link or a mention (F13
    changed paths).
15. Forward and copy, and the forward header checked in a Telegram app (F14).
16. One album, approved and read back; how the clients show several captions (0.56).
17. A caption edit (plain, removed and styled), a styled text edit, a pin and an unpin (also in a forum topic, a
    private chat or Saved Messages under Telegram's rules), a deletion of one own message in a private chat and in a
    group, the Saved Messages and other-people refusals, and the server's answer for an already self-deleted
    message (0.57).
18. Replies to a voice note captioned with a timestamp, to a custom emoji source and to a media caption with a
    hidden link; one approved styled-caption send with its readback (0.58).
19. Installed file and page coverage on real artifacts: PDF pages, XLSX, PPTX, DOCX and text (0.54 row F; F8, F9).
20. Two conversational tasks on the owner's account, with and without the skill (0.54 row F).
21. The consumer-matrix owner cells: real-account runs through the official SDK, the Inspector, Codex and Claude
    Code (and Claude Code's synthetic cell if `claude -p` cannot run headless), Claude Desktop and the plugin.

**E. Second account** (0.55)

22. `account-add` an alias, `connect --account` with the second account's phone; one approved action on each
    account; the dialog names the right alias and masked account; `status` per account.

**F. Migration of the owner's daily install** (0.53, 0.54)

23. `--adopt-legacy` from the signed kit with the recipe of the 0.53 spec, including the Claude Desktop and Codex
    entries the installer does not own.
24. The capability-profile switch on the installed bundle, and a real upgrade and rollback of it (F18).

**G. After the session**

25. Defects found: fixes, then a rebuild and re-sign; the lead re-runs the rows they touch.
26. The lead runs the final G1 (the gate with the bundle stage, the full consumer matrix, the history and identity
    reports) and writes the final release notes and roadmap.
27. The history decision and the acceptance of the history findings; the legal review; Gpub naming the refs, the
    artifacts and the published version.
