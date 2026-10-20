# Completion plan — from 0.52.0 to the end of the original plan

**Status:** lead plan, 20.10.2026. Written under the owner's standing delegation to finish the
[original plan](initial-roadmap.md) autonomously. Every choice below is a **lead decision, owner review
pending**; the owner reviews the consolidated decision sheet at the end. Live status stays in
[the roadmap](../roadmap.md); each release gets its own `docs/plans/<version>-<slug>-spec.md` with a
`.tasks.md`.

Basis: a read-only gap map of eight areas (F13, F15b, F16, F17, F18/F19, client coverage, conversational
usability, release process), a planner and a completeness critic. The raw map is kept outside the
repository because it quotes local operational details.

## Release sequence

Each release closes with a machine-checkable technical gate. After 0.52.0, releases are tagged locally as
annotated release candidates (`vX.Y.0rc1`). Real provider rows that need the owner (approval-dialog
clicks, QR scans, a second account, a clean macOS user, signing) run once, in a single prepared owner
session against the final candidate. Final versions are cut after that session.

| Version | Theme | Main items | Gate (summary) |
| --- | --- | --- | --- |
| 0.52.0 | Release the QR-first candidate | deterministic suite; tracked `packaging/release_gate.py` with re-derivable evidence; privacy guard over the public tree; version bump; release notes with every unmet 0.52 gate bullet disclosed; constraints file; frozen 0.52 schema-hash fixture; `docs/releasing.md` | `release_gate.py run` exits 0 on the release commit; `verify` re-derives commit, tree, sdist and wheel hashes from `git archive <commit>` |
| 0.53.0 | Client coverage and distribution | tracked reproducible payload builder; lock v2 grammar; Developer ID signing, notarization and stapling pipeline (proven with a dry identity, final run by the owner); Claude Desktop registration; Claude Code plugin and marketplace; policy-filtered `tools/list` (registration fingerprint unchanged); product-identity module for a later rename | full suite; payload built twice is identical; bundle stage of the release gate; installer tests for each client |
| 0.54.0 | Conversational usability | tool-description budget; lean skill; fixed recovery hints; split `not_approved` reasons; clearer approval dialog; scripted conversation scenarios through the official SDK | surface and skill budget tests; conversation scenario suite |
| 0.55.0 | F16 multiple accounts | `AccountPaths` with the legacy root as alias `default`; one broker and LaunchAgent per alias; alias fixed in a per-alias registration; account-bound handles everywhere | default alias byte-identical to 0.52 paths; isolation matrix over every handle kind |
| 0.56.0 | F15b ordered albums | album drafts, per-item preview, `sendMessageAlbum` closed shape, per-item outcomes, `partial` status, no whole-album retry | album contract and observation tests |
| 0.57.0 | F17 rest | own caption edit, styled text and caption edits, pin/unpin, exact deletion of one own message; each its own default-off capability requiring `send` and `read` | negative allowlist tests; dialog goldens; final-state verification tests |
| 0.58.0 | F13 rest | TextUrl captions, CustomEmoji and MediaTimestamp source families, formatted captions; the reply-source version ladder consolidated before it grows further | v1–v23 goldens unchanged; family capability conjunction tests |
| final | F18 matrix, TDLib procedure, F19, final audit | installed consumer matrix (official SDK, Inspector, Codex, Claude Code, Claude Desktop); controlled TDLib upgrade procedure and rehearsal; F19 transport-scope regression test; requirement-by-requirement audit of P0–P3 and F1–F19 | agent cells recorded; owner session rows; final G1 evidence; Gpub only with the owner |

The order after 0.54 follows the owner's stated order (F16, F15b, F17, F13). Behaviour-neutral enablers
that later slices need (a single draft-kind table, a shared caption freezer) may land early as
preparation commits.

## Lead decisions (owner review pending)

- **Commit identity.** New commits use the public lineage identity of v0.51.0 through a repo-local git
  config. Earlier commits since v0.51.0 carry a personal address; their remediation is an owner decision.
- **Publication hold.** `main` is not fast-forwarded on the remote and no tag is pushed until the owner
  decides the history question. A fast-forward would make the cheapest remediation (rewriting only the
  unmerged branch range) impossible without rewriting `main`. Working branches are pushed as backups.
- **Evidence chain for 0.52.0.** Each real-evidence row is carried to the tag with its own diff from the
  row's commit, every changed file classified. The release notes state which real sends predate the text
  rewrite (0b29806).
- **Hash fixture scope.** The schema-hash fixture covers input/output schemas and annotations only;
  descriptions are guarded by a separate budget test.
- **Tool census.** Counts are defined on the full registration under a full-capability synthetic policy;
  the policy-filtered listing is asserted separately.
- **Capabilities.** Every new action is its own default-off capability; state-changing ones require
  `send`, and those whose preparation reveals content also require `read`.
- **TDLib and transport.** Stay on d1085f9 / 1.8.67 unless a concrete trigger appears. F19 Streamable
  HTTP stays not applicable, guarded by a regression test.
- **Network.** Only pinned, hash-verified development inputs are downloaded. No remote write other than
  pushing working branches happens before Gpub.

## Owner decision sheet (prepared, for the end)

1. Review all lead decisions marked "owner review pending" across the specs, including the release order.
2. Public history and commit identity: accept as is (A), or rewrite the unmerged range to the public
   identity with redactions and force-push the branch before `main` moves (B). Rewriting `main` (C) is not
   recommended.
3. Product name, and whether the MCP server name and skill ids change with it.
4. API identity of the public bundle (the current one or a dedicated application). A change refuses
   existing enrollments, so it is decided before any end-session evidence.
5. Signing on the build Mac: a Developer ID Application certificate in the login Keychain and a
   `notarytool` keychain profile.
6. Legal: Telegram API terms, the third-party notice set and the AGPL corresponding-source claim.
7. Real fixtures on a test account: a small group, a forum supergroup with an open topic, captions with
   links, and a Premium sender for CustomEmoji if available.
8. Clean macOS user run with the signed build (install, QR, 2FA, interruption, logout/login, Claude Code
   and Codex searches).
9. One real-evidence session: approve the dialogs listed in the runbook (F13, F15b, F17, the F14 forward
   header check, the redesigned approval dialog).
10. F16 two-account validation: scan the second account's QR and approve one action per account.
11. Migration of the owner's daily install, including Claude Desktop and Codex entries the installer does
   not own.
12. Gpub: an explicit authorization naming the refs and artifacts to publish.

## Top risks

- The end session concentrates most real rows; a defect found there forces a rebuild and re-sign.
- Hardened runtime and a Developer ID python can change the code identity behind Keychain ACLs.
- Hot files (server.py, broker.py, tdjson.py, outgoing_drafts.py, send_observations.py,
  approval_prompt.py) change in every release, so integration stays serialized.
- Privacy and publication steps are irreversible; GitHub keeps unreferenced objects.
- Timing-sensitive tests must be made deterministic before the gate is trusted.
