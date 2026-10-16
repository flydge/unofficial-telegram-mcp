# Overnight development log — 16.10.2026

**Summary:** 0.52 QR-first onboarding phases A–E are implemented and reviewed (phase F needs you);
three new opt-in features — F14 forward/copy, F15a native video, F17a archive/unarchive + own text
edit — are implemented, reviewed twice where they touch sending, and documented. Tools 47 → 55,
tests 2053 → 2614 passing (3 skipped), 32 commits on `qr-onboarding-052`, nothing pushed.

Branch `qr-onboarding-052`, local only (nothing pushed). Every phase: implementer → lead
re-runs the full isolated-HOME suite → independent critic → fixes → commit.

## Done

| Commit | What | Evidence |
|---|---|---|
| bed65dc | Pin `segno==1.6.6` (QR rendering) + BSD notice | packaging tests green |
| 2ced778 | Fixes from the phase A+B critic (1 medium, 9 low) | suite green; new tests for each fix |
| 7d1f914 | Spec amendment: partial Keychain pairs, legacy binding, Ready vs cancel, 5 s password budget | — |
| 15c27df | F14 forward/copy spec (decisions delegated, **owner review pending**) | — |
| f7b8ed4 | Phase C: loopback QR screen (RU/EN), guards, CSP | 49 new tests; suite 2174 passed / 3 skipped; screens checked in browser |
| c89419f | Phase C hardening from the security critic (connection cap, absolute deadline, close ⇒ cancelled, no SO_REUSEPORT, CORP/COOP) | new tests; no cross-origin path to QR/password found |
| fc1c915 | Phase D: `connect`/`wait`/`api-credentials` CLI, transient launchd helper, guarded reconnect | 77 new tests; suite 2257 passed / 3 skipped (not run against real launchd/osascript) |
| cd4c270 | Phase E: bundled identity flag, Claude Code registration, `~/.claude/skills`, activation → `connect`, setup skill + docs; client roots accept 0755 (was a hidden blocker for every real user) | suite 2304 passed / 3 skipped; real `claude mcp get` format checked read-only |
| 34ca501 | Spec: headless `next_action` aligned with code | — |
| 2401612 | Phase E review fixes: Claude state from `~/.claude.json`, CLI write-only with no-follow locate + minimal env, non-blocking outcomes with a manual command, no re-run after receipt, overlap check, `~/.claude` skills not re-verified at launch | 144 focused tests green |
| e271bba | Phase D review fixes: revoke ⇒ blocked, restore broker after failed reconnect, `run/connect.lock` single helper, `wait` expiry, hung/linger handling, helper launchd-only | full suite 2360 passed / 3 skipped |
| df71ac9 | 0.52 phase F marked deferred to owner | — |
| 34e6a16 | **F14 forward/copy of one message** (`prepare_forward_send`, `send_prepared_forward`, default-off `forward_send`) | 30 new tests; legacy tool hashes unchanged; critic review in progress |
| 1bd216c | 0.52 integration fixes: launcher refusal codes, `broker_starting`, launch checks independent of client config, flood_wait retry, full next_action table + skill, sandbox fallback, payload gate, `wait` per phase change, new `cancel` | 428 focused tests green |
| b7c3954 | F14 review fixes: refuse hidden content (TextUrl etc., URL buttons, unseen previews), forward-of-forward, auto-delete; bind signature/via-bot/audio names; supersession; validated results | 50 forward tests; full suite 2496 passed |
| d741056 | Forward dialog readable in any script (Cyrillic was shown as \u escapes); invisible/bidi/look-alike chars marked ⟦U+XXXX⟧ | 53 forward tests |
| c6908fd / 004a320 | F15a native video spec (delegated decisions) + stdlib MP4/MOV probe | 51 probe tests incl. mutation fuzz |
| fd47384 | F14 docs (reference, security allowlist + boundary, install, search skill, roadmap, changelog, README; candidate = 49 tools) + spec alignment | — |
| ccaa3ed | **F15a native video send** (`prepare_video_send`, `send_prepared_video`, default-off `video_send`) + F14 re-review fixes (more invisible/look-alike marks, « » spans, exact link-preview URL rule, altered names refused) | full suite 2542 passed / 3 skipped; tools 51 |
| 836bfc5 | F17a archive/unarchive + own text edit spec (delegated) | — |
| 1b81cfe | F15 review fixes: attempted artifact/video send never reported `expired` (also fixed for plain documents), correlation-only video confirmation, preview-before-draft, dialog spans | full suite 2551 passed / 3 skipped |
| 2e81b02 | F15a docs (51 tools) + spec alignment | — |
| fdd6940 | Docs audit fixes: stale 'paused' text removed, candidate capability-enable instructions (was a dead end), no over-claim of Claude Code registration, QR boundary in security.md, spec corrections | — |
| 627129b | **F17a archive/unarchive one chat + edit one own text message** (4 tools, default-off `archive_chats`, `edit_own_text`) | 45 new tests; full suite 2597 passed / 3 skipped; tools 55; critic in progress |
| 4613444 | F17a review fixes: refuse edge whitespace and TDLib-rewritten characters in edits, 5xx ⇒ outcome_unknown, sponsored positions excluded, fuller dialogs, all 19 critic mutations killed | full suite 2604 passed / 3 skipped |
| 1c0a077 | F17a docs (55 tools) + `prepare_text_edit` description aligned with code | — |
| 4c4ca76 | Final review fixes: forward/video race loser ⇒ `outcome_unknown`, `forwardMessages` refused on generic call path, forward preview before draft, 0.51 rollback + browser-history notes | **full suite 2614 passed / 3 skipped** |

## Waiting for the owner (cannot be done without you)

1. **0.52 phase F — real QR acceptance** on the separate macOS user with the test account
   (spec criteria 9–13): install via Claude Code → QR scan → 2FA → Ready → search in Claude Code
   and Codex; QR refresh, cancel, interruption; restart without login; revoke → reconnect.
2. **Bundle build with the identity.** Run assembly with `--app-identity-from-keychain` yourself
   (it reads your Keychain pair locally; I never read it). Product API identity at bundle build: confirm the Keychain pair to embed (you chose your
   current api_id); the assembly flag reads it locally.
3. **F14/F15/F17a real checks**: one approved forward, one copy, one native video, one
   archive/unarchive of a test chat and one edit of your own test message (needs a broker with
   `send` plus `forward_send`/`video_send`/`archive_chats`/`edit_own_text` in a test policy).
4. **Review delegated decisions** in `docs/plans/0.53-forward-copy-spec.md` and
   `docs/plans/0.53-native-video-spec.md`, `docs/plans/0.54-archive-and-edit-spec.md` (including the readable dialog rule and its limits:
   pure-script look-alike words are not marked; ß/ł/ø mixed with ASCII are marked).
6. **Pre-existing, for a later decision:** plain text sends accept characters TDLib rewrites;
   plain text/artifact send race losers still read `expired` (forward/video/archive/edit don't).
5. **One scope decision (F15 M1):** only the call that made a send attempt is protected from a
   misleading `expired`; a *later* call after the authority was lost still reads `expired` (30
   existing reply tests pin that). Widening it is your call.

## Notes and risks found overnight

- **Final whole-branch review** (all ~50 commits): nothing above low. No pre-existing tool schema
  changed; no new capability is enabled by default (fresh 0.52 policy or legacy no-file default);
  cross-kind isolation sound; no new secret leak. Lows (forward/video race loser read `expired`;
  0.51 rollback after a 0.52 state write) are fixed/documented in the last commit.
- **Pre-existing behaviour found (not changed):** the pinned TDLib silently rewrites some
  characters inside message text (tab → space; U+2028–U+202E, U+030A, U+0333, U+033F removed).
  Edits now refuse them; plain text sends still accept them (recorded in the F17a spec).
- **Test hygiene:** bootstrap test runs leave owner-only `/private/tmp/telegram-bootstrap.*`
  directories behind (0700/0600; likely synthetic values). Safe to delete; not deleted by me.
- **Parallel lineages exist outside this checkout.** `/Users/flydge/tg-dev-1 … tg-dev-26` are separate
  clones on `qr-onboarding-052`/`qr-onboarding-phase-d`, branched from this repo's phase B commit
  `0a49f6c`, with independent implementations of phases C, D and (in tg-dev-7/8/9) E, dated
  15.10 (tg-dev-20 changed at 01:01 on 16.10). Codex research folders reference them. They were
  not reviewed, merged or modified overnight; this checkout stays the canonical line. If you ran
  them as a comparison, `tg-dev-9` (`882bf35`, phase E gate) and `tg-dev-15` (phase D evidence)
  are the furthest.

- **Critic reviews of D and E found real problems, all being fixed before moving on:** D —
  after a phone-side revoke the broker reported `error` (not `blocked`) so reconnect could not
  trigger; a failed reconnect could leave the broker booted out; `wait` could loop on a stale
  status; two `connect`s could race. E — an npm-installed `claude` (needs node on PATH) would be
  misread as "no entry", half-publish the install and lock it after 8 retries; registration now
  reads `~/.claude.json` (verified shape on this Mac) and never blocks the install.
- **Cross-phase integration review** (after all phases): 1 high (launcher hid installer refusal
  codes), 8 medium (own slow-starting broker shown as `owner_busy`; every launch depended on
  exact Codex config/skill dirs; flood_wait lost its retry time; incomplete next_action table;
  sandboxed agent shell got the wrong fallback; helper start failures looped; bundle assembly did
  not prove the payload carries this code; headless/blind 9-minute wait). Fixes in progress with
  a new agent-safe `cancel` command and `wait` returning on each phase change.
- **F14 critic:** no critical path (no unapproved, double or retried send). One high: hidden
  parts of a message (TextUrl targets, URL buttons, unseen link-preview URLs) would be sent but not
  shown — now refused. Medium: attribution of already-forwarded sources, lossy sanitized display
  (homoglyph/bidi), no supersession — being fixed.
- Spec §2 rule "one resolution function" is deliberately not applied to the Terminal setup path,
  which keeps its pre-0.52 partial-pair resume (amended).
