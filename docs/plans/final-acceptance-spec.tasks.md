# Final acceptance — tasks

Spec: [`final-acceptance-spec.md`](final-acceptance-spec.md).

- [ ] Criterion 1: Full isolated-HOME suite green with 0 skipped (suite).
- [x] Criterion 2: Every synthetic and agent matrix cell PASS with a bound receipt; a deliberate schema change fails it (harness).
      Extended 26.10.2026 by seven synthetic cells (55) and the listed schema change `0.59-text-send-capabilities`
      (below): the synthetic and Inspector columns pass at 2755559; the agent columns were not re-run, and the receipt
      at fc3f3c8 no longer verifies against the current commit (its schema fingerprint moved), so the lead runs the
      whole matrix again at the release candidate.
      Met (25.10.2026, after the F18 review fixes below): 48/48 synthetic, Inspector 4/4, Codex 9/9 and Claude Code
      9/9 in one receipt at fc3f3c8, verified by `check --commit fc3f3c8…`. Part 2's judgement that the Claude Code
      column was not applicable (it never ran: an empty configuration directory cannot reach the Keychain login) is
      withdrawn; the column now logs in through Claude Code's default configuration and loads none of it. Lead
      decision, owner review pending.
      - [x] Part 1, synthetic and Inspector columns (25.10.2026, F18 part 1; spec
        [`final-consumer-matrix-spec.md`](final-consumer-matrix-spec.md)). Harness and tests at 4be1e95 (cb238c6,
        then the bundle cells' server name read through the product identity): 48 synthetic cells (every one of the
        64 tools through 0.58, every guard and argument refusal text, the 0.58 reply families'
        `missing_capabilities`, cursors, caps, a scripted dialog timeout, owner denial of all eleven send and apply
        families, and the installed bundle installer's install, upgrade, rollback and uninstall over a synthetic
        home with a fake launchctl) and the MCP Inspector CLI column (4 cells). Run at 4be1e95 against the 0.58.0rc1
        gate wheelhouse and toolhouse (`rc-0.58.0rc1-6d1680d`), Inspector `@modelcontextprotocol/inspector-cli`
        1.0.2 on Node 22.18.0, installed offline from the cache `inspector-fetch` filled (lock sha256
        `b7bcccd6fe10b8c9…`): `consumer matrix passed: 48/48 synthetic cells; Inspector 4/4 cells; 9 owner cells
        pending`. Receipt `tgmcp-consumer-matrix/v3` `matrix-final/receipt-0.58.0rc1-4be1e95.json` beside the gate
        directories, sha256 `6b0af8b27e03199f4385a2acce311a3bfbf606d0d09897da0078e4d3d713ea42`: wheel
        `6c444765bfcae9799ada89ce3d2bc7fec2cc8985fb172ecdda850da226321c12` (2,186,221 bytes), schema fingerprint
        `6a0b0971d74d988c60399ed4019db7d8ec97c9997ea5713d9074e9713b1bfe7d`, 64 tools, 157 structured results from
        all 64 tools, installer tomlkit 0.13.3 from the gate toolhouse, scripted approval. `check` with the same
        wheelhouse rebuilt the wheel, reinstalled the closure and re-listed the tools:
        `consumer matrix receipt verified: 48/48 synthetic cells; Inspector 4/4 cells; 9 owner cells pending`.
        (The same run at cb238c6, receipt sha256 `0135a446…`, passed and verified too; superseded by 4be1e95.)
        A deliberate schema change fails both: `SchemaChangeTests` patches the registration (an input bound, an
        extra tool) and `run` refuses `schema_mismatch` before any session, `check_receipt` refuses the old receipt
        against the patched census and a receipt over the patched schema on its own (the frozen
        `MATRIX_SCHEMA_FINGERPRINT`). The new tests failed first against the 0.58.0rc1 harness (collection error:
        no Inspector, tool tables or v3 receipt).
      - [x] Part 2, Codex and Claude Code agent cells (25.10.2026; spec sections 7a, 8 and 9). Harness and tests
        at 6f53134: receipt `tgmcp-consumer-matrix/v4` adds `agents`; nine cells per column (census through the
        model, a read, a search paged by its cursor to the end, a reply refused `capability_disabled` whose
        `next_action` the model must follow, an approved send whose owner dialog times out, an alias server, a
        documents album prepare, an own-message edit prepare, a reply to a 0.58 reply-family source), each the real
        CLI with a private configuration home whose only MCP server is the installed proxy, against its own synthetic
        broker, decided on the broker's own transcript; evidence kept beside the gate directories and bound by
        digest, judged again by `check`. The new tests failed first against 6b1b276's harness (collection error: no
        agent columns); they run both columns from the tree with a stand-in agent (honest and misbehaving).
        Run once at 6f53134 against the 0.58.0rc1 gate wheelhouse and toolhouse (`rc-0.58.0rc1-6d1680d`), Inspector
        1.0.2 on Node 22.18.0 from the same offline cache (lock sha256 `b7bcccd6fe10b8c9…`), `codex-cli 0.162.0`
        with model `gpt-6.1-sol`, Claude Code 2.1.304: `consumer matrix passed: 48/48 synthetic cells; Inspector 4/4
        cells; Codex 9/9 cells; Claude Code unavailable (not_logged_in); 7 owner cells pending`. Receipt
        `matrix-final/receipt-0.58.0rc1-6f53134.json`, sha256
        `ab7d8448e2b6051505e8c335a6fbfbc52899c8743eb09b7d44bfbf4cca672066`: wheel
        `30da8673990d027fbdf73a9887844749a1b2027cb337db1c9546d481c4fccb69` (2,186,221 bytes), schema fingerprint
        `6a0b0971d74d988c60399ed4019db7d8ec97c9997ea5713d9074e9713b1bfe7d`, 64 tools, 157 structured results from
        all 64 tools, installer tomlkit 0.13.3 from the gate toolhouse, scripted approval. Evidence
        `matrix-final/agents-6f53134/` (owner-only): every Codex cell passed on its first attempt with no advisory
        failure (12–25 s each); the broker transcripts show, for example, `get_message_context` then the refused
        `prepare_reply_text_send` and nothing else in the refusal cell, `prepare_text_send` then a `timeout` send in
        the timeout cell, and three `search_messages` pages in the cursor cell; no provider-changing request in any
        cell. The Claude Code probe (first cell, not bound) answered `authentication_failed` / "Not logged in ·
        Please run /login" with no tool call. `check` with the same wheelhouse and evidence rebuilt the wheel,
        reinstalled the closure, re-listed the tools and judged every agent cell again: `consumer matrix receipt
        verified: 48/48 synthetic cells; Inspector 4/4 cells; Codex 9/9 cells; Claude Code unavailable
        (not_logged_in); 7 owner cells pending`. Codex did not refresh its tokens during the run (the caller's
        `auth.json` is unchanged); the private homes were deleted. The schema-change tests (`SchemaChangeTests`)
        still fail run and receipt check as required, a receipt with passed agent cells included. (Superseded by the
        receipt at fc3f3c8 below.)
      - [x] **F18 review fixes** (25.10.2026; review `final-f18`, nine confirmed findings; spec sections 7a, 8 and 9).
        Each fix has a test that failed first against the commit before it.
        1. *A login lost mid-column hid an earlier failed cell* (medium) — c1b7d87. Only a login failure on the
           column's first cell, before any frame reached the broker, makes the column `unavailable`
           (`not_logged_in`); a later one keeps every result, fails the column (run refuses `agent_failed`) and
           prints a fixed line. Test `test_a_login_failure_after_the_first_cell_fails_the_column_and_keeps_every_result`
           (a cell failing `only_matrix_tools`, then a login failure; and a passed cell, then a login failure): failed
           first with `'unavailable' != 'fail'` for both columns.
        2. *SIGTERM or SIGHUP during an agent cell left the agent, the broker, the temporary home and the copied
           credential behind* (medium) — baae916, fc3f3c8. `run` and `check` install handlers that kill every running
           agent's session and raise once, so every finally runs (brokers and drivers stopped, homes and the copied
           credential removed, a refreshed credential still returned); children are started and handed to their
           cleanup with the raise deferred, a repeat signal never cuts the cleanup short, and the command prints
           `interrupted` and exits 128 + the signal; fc3f3c8 creates every temporary home inside the try that removes
           it. Test `test_sigterm_or_sighup_during_an_agent_cell_leaves_nothing_behind` interrupts a real harness
           process while a real child agent sleeps for 300 s: failed first with `[True, True, True, True]` (agent
           alive, broker alive, `/private/tmp/tga.*` home and the scratch with the copied `auth.json` left) for both
           signals; the four homes those failing runs leaked were removed. A real interrupt of the first matrix run at
           5ada42c (SIGTERM during its build) ended with `interrupted`, exit 143 and no temporary directory left
           (`matrix-final/run-5ada42c.log`).
        3. *Retry and login markers matched the model's own words; a retry deleted the previous attempt* (low) —
           808183b. Classification reads only stderr and provider error events (Codex `error`, `turn.failed`, error
           items; Claude Code API error messages and failed results); every attempt is kept in
           `<column>/<cell>/attempt-<n>/`, the receipt cell records `attempts`, and `check` verifies the bound
           attempt and that each earlier one is kept and reached no broker. Tests (prose naming login and capacity
           problems fails `broker_checked` on attempt 1, not `unavailable`; the transient retry keeps both attempts;
           the cell's `attempts` is closed): 8 test functions failed first.
        4. *check did not bind to the release commit* (low) — 24e4d3e, 5ada42c. The verified line names the
           receipt's commit and wheel sha256; the releasing recipe and checklist pass `check --commit <release sha>`.
           Failed first: the verified line had no commit.
        5. *The 0.54 spec's pointer said receipt v3* (low) — 5ada42c: "receipt v4 (v3 in part 1), the Inspector and
           agent columns".
        6. *The Codex credential write-back was not exception-safe or validated* (low, low) — 7969d1c. A refreshed
           `auth.json` goes back only as a JSON object with the caller's top-level keys and `tokens` keys whose
           non-empty fields stay non-empty (`invalid` otherwise); a failed write unlinks its temporary file and keeps
           the caller's file (`failed`; an interrupt is re-raised after the unlink); the column's scratch is removed
           in its own finally. 3 tests failed first (only a well-formed refresh goes back; a failed write keeps the
           caller's file and leaves no temporary file; the scratch is removed when the write-back raises).
        7. *The write-back was not stated where operators read* (low) — 09879ea, 5ada42c. Decision: both. The
           write-back and its exact condition are stated in releasing.md and the spec's isolation bullet, and
           `--codex` now requires an explicit absolute `--codex-home` (`agent_invalid` otherwise; no default from
           `CODEX_HOME` or `~/.codex`), so the operator names the file that may be written back.
        8. *Evidence, work directory and receipt were allowed inside the repository* (low) — 09879ea. `run` refuses
           each inside the repository or any git work tree (`.git` entry in the path or a parent) before creating
           anything: receipt `usage`, `--workdir` `workdir_invalid`, `--agent-evidence` `evidence_invalid`. 2 tests
           failed first; the old code had created an empty `matrix-evidence` folder in the checkout (removed).
        9. *The Claude Code column never ran, yet criterion 2 was ticked* (medium) — 4d8a869. Decision: run it through
           Claude Code's default configuration for the login only, loading none of it (spec section 9). Checked first,
           with snapshots of the size, mtime and sha256 of `~/.claude.json`, `~/.claude/settings.json`,
           `~/.claude/settings.local.json` (absent) and the list of `~/.claude/projects` before and after each probe:
           `claude auth status` reports no login with an empty HOME (with or without `CLAUDE_CONFIG_DIR`) or without
           `USER`, and the claude.ai login with the caller's HOME and USER (snapshot unchanged). A first `claude -p`
           probe with `--setting-sources ""` (and no `USER`, so not logged in) created an empty
           `~/.claude/projects/<temp dir>/memory` auto-memory folder, which was removed; with `--settings
           '{"autoMemoryEnabled":false}'` added, the probe answered `NONE` when asked which CLAUDE.md, AGENTS.md or
           memory files were in its context, its init event listed no built-in tool, MCP server, skill or memory path
           and only Claude Code's two built-in plugins (the user's enabled plugins absent), and the snapshot was
           unchanged; one source-runtime census cell then passed the same way. The only other change seen under
           `~/.claude` was Claude Code's runtime session registry (`~/.claude/sessions`, an entry added and removed
           by each run) and `~/.claude/system.json`, which also changes while nothing runs. The harness now passes
           the caller's HOME and USER, `--setting-sources ""`, the auto-memory setting, `--disable-slash-commands`,
           `--strict-mcp-config` with the temporary `--mcp-config`, `--tools ""`, `--no-session-persistence`,
           `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_AUTOUPDATER=1`, and refuses the run
           (`client_config_changed`) when the guarded snapshot differs after the version check or after any cell.
           4 tests failed first (environment, command, guard).
        **Run** once at fc3f3c8 (the harness after every fix; later commits change docs only) against the 0.58.0rc1
        gate wheelhouse and toolhouse (`rc-0.58.0rc1-6d1680d`), Inspector 1.0.2 on Node 22.18.0 from the same offline
        cache, `codex-cli 0.162.0` with model `gpt-6.1-sol` (rerun so one receipt binds both columns) and Claude Code
        2.1.304 with model `claude-opus-5-5`: `consumer matrix passed: 48/48 synthetic cells; Inspector 4/4 cells;
        Codex 9/9 cells; Claude Code 9/9 cells; 7 owner cells pending`. Receipt
        `matrix-final/receipt-0.58.0rc1-fc3f3c8.json`, sha256
        `77b8230fa01ab4734ffc6458ff2d7994c0fb67f429bad37d9a552ec649578118`: wheel
        `ac8d8cfa729757f3acafbb4cfd455d480953763bda43d5ed185842143e33bc3d` (2,187,115 bytes), schema fingerprint
        `6a0b0971d74d988c60399ed4019db7d8ec97c9997ea5713d9074e9713b1bfe7d`, installer tomlkit 0.13.3 from the gate
        toolhouse, scripted approval. Evidence `matrix-final/agents-fc3f3c8/` (owner-only): all 18 agent cells passed
        on their first attempt with no advisory failure (Codex 9.9–20.0 s, Claude Code 7.4–18.7 s per cell); every Claude
        Code init event lists only the matrix server's tools, no skill, no memory path and only the built-in
        plugins. The broker transcripts show the same shape in both columns, for example only the refused
        `prepare_reply_text_send` in the refusal cell, `prepare_text_send` then a `timeout` send in the timeout cell
        and three `search_messages` pages in the cursor cell; no provider-changing request in any cell. `check
        --commit fc3f3c81380f8606a028df546cc1c687ccebfbe0` with the same wheelhouse and evidence: `consumer matrix
        receipt verified: 48/48 synthetic cells; Inspector 4/4 cells; Codex 9/9 cells; Claude Code 9/9 cells; 7 owner
        cells pending (commit fc3f3c81380f8606a028df546cc1c687ccebfbe0, wheel sha256
        ac8d8cfa729757f3acafbb4cfd455d480953763bda43d5ed185842143e33bc3d)`. Logs `run-fc3f3c8.log` and
        `check-fc3f3c8.log` beside it. Codex did not refresh its tokens (no write-back line; the caller's
        `auth.json` is unchanged), the guarded Claude Code snapshot taken before and after the whole run is
        unchanged, and no `/private/tmp/tga.*`, `tgcm.*` or `tgmcp-matrix-*` directory remained.
- [x] Criterion 3: TDLib procedure written; rehearsal reproduces d1085f9 natives and lock byte-identically (rehearsal record).
      Met in one reading, which the owner confirms (owner decision sheet, item 13): independent rebuilds are
      byte-identical to each other, not to the shipped natives.
      The procedure is [`docs/tdlib-upgrade.md`](../tdlib-upgrade.md); the rebuild route is
      `packaging/build_tdlib_source.py` (c14e37d, 4fffce5, 76d6bc5; review fixes 48600cc, 4783c0b, 0c4d9b8,
      none of which changes what a build writes). The [rehearsal record](final-tdlib-rehearsal.md) shows
      clean builds at three build paths, 25.10.2026: the natives (libtdjson `46e04c99…60a7`) and the
      lock-shaped document (`72b8e0cf…33cf`) are byte-identical to each other, and every output is equal.
      The shipped, Homebrew-derived pin (libtdjson `65ef0643…a12c`, lock `489b8ea9…c1ab`) is reproduced by
      `prepare_native.py` from the installed keg, a re-relocation and not a rebuild; the source-built
      libtdjson differs from it, which is recorded as information. The review's ten points are fixed and
      recorded in the record's "Review fixes" section, including the owner's session backup and restore
      for a TDLib that migrates the session database. Not exercised, because there is no candidate and no
      trigger: steps 1 and 5–7 on a new commit, including the owner's real read-only check and a real
      session restore.
- [x] Criterion 4: F19 transport pins pass (unit tests) — `tests/test_transport_scope.py`, 823ccf7.
- [ ] Criterion 5: Audit table complete; critic finds no unresolved applicable gap (critic report).
      Audit written, 25.10.2026: [`final-audit.md`](final-audit.md), one row per requirement (P0–P3, F1–F19), with
      the release gates and the open items; the consolidated [owner decision sheet](owner-decision-sheet.md) beside
      it (98fd934, b880f85, 4d53766, 0c9f040).
      **Critic ran, 25.10.2026** (independent, two verifiers per finding; report kept outside the public tree): 22
      confirmed findings (one high, eight medium, thirteen low), one contested low that one verifier upheld, one
      refuted. All 23 are fixed in 5fdbe37 (audit) and b141c77 (decision sheet):
      - Status rules applied strictly: F6 and F9 partly met, each with its agent-side gap (installed-consumer calls
        of the Boolean form and the sender, direction and topic filters; reads of a synthetic XLSX and PPTX); F11
        met with owner remainder (the real dialog rows); F7 and F16 cite the interim F18 cells that exercise them
        (`search_chats` and its cap; the real Codex alias server); F2's matrix citation corrected (the receipts up
        to 0.58.0rc1 call `get_message_context` only for refusals).
      - Evidence caveats: the 20.10 captioned video send (F15) and own-text edit (F17) predate 0b29806, with owner
        rows for both; the 057 formatted-send confirmation is a changed send path (F13) with an owner row; P3 and
        the gate table say the 0.53.0rc1 and rc2 history findings were not recorded; the rc2 gate row names its
        tag annotation, and open item 8 the stale 0.53 rc2 section; the pushed working branches already expose the
        unpublished range (P3, Gpub, decision 2); `forward_send` and the reply sends reveal content without `read`
        (F14, shared limits, sheet section 2); the never-reachable line states the allowlist and `_call` precisely
        (a shape-valid `sendMessage` passes `_call`); the 0.54 and 0.57 owner rows are in F17 and F18.
      - Decision sheet: the history decision first in phase A, because option B rewrites the commits the signed
        build is tied to; the published version decided before the build (decision 16); the API identity and the
        legal review before the build, with the no-identity consequence for the 0.52 gate; options and
        consequences for every decision; a localisation slot; the complete fixture list; owner-only rows stay
        with the owner after a rebuild and the final G1 includes the real scenarios; the 0.53 rows in the history
        table; the mounted-DMG run moved to the lead.
      - The mounted-DMG run (P2's agent-side gap) is done, below. F15 and F17 move to met with owner remainder on
        the interim F18 cells, as the audit's open item 2 foresaw.
      Counts after the fixes: 10 met, 7 met with owner remainder, 6 partly met, none not applicable or not met.
      The reconciliation of the rows that cited the interim F18 receipt and an independent check of the fixes
      followed:
      **Re-critic ran, 26.10.2026** (independent, two verifiers per finding, over the fixes at ccf938a; report kept
      outside the public tree): 15 confirmed findings (two high, seven medium, six low), none contested or refuted.
      All 15 are fixed in ff8fdee (audit) and e1e29d3 (decision sheet):
      - High: the final notes, changelog and roadmap must be in the build commit (pre-cut conditions; the notes and the
        roadmap ship in the wheel; the release commit changes only the plugin's pins), so decision 16 and steps 14 and
        37 carry the lead's model: written in phase A with every owner row pending or deferred, the session's
        outcomes in the release record or the release text, a second signed build only when the session forces a
        code change. Every build writes `publication: unreleased`, so the plugin and curl paths refuse after Gpub:
        new decision 17 in phase A, tied to the legal review (complete the source inventory, or publish only the DMG
        with those paths refused for this version); P2's code check and open item 12.
      - History: the 0.53.0rc1 and rc2 findings listed from the gate evidence the tag annotations name (two in
        9712dc1, replaced by e9dba5c before the rc1 tag; described, never quoted), with the 0.52.0 counts (32 in 11
        commits); a lead step before decision 2 runs the history and identity report over `v0.51.0`..tip; the
        acceptance of the itemized findings moves into phase A, with the full cost of a remediation after the build;
        the build commit's gate and the final G1 run from `v0.51.0`; the remote `main` already holds six post-`v0.51.0`
        commits with the personal identity, which option B does not reach (decision 2, P3, How to read, Gpub). Found
        while listing them: the 0.54.0rc1 evidence holds three findings (one synthetic chat id on three lines of
        cfe5dc3), not one; the audit and the sheet now say three.
      - Session order: a pre-publication route for the browser-downloaded DMG (a local HTTP server opened in Safari on
        the clean user) and for the plugin (the kit's `bootstrap.sh --claude-code plugin` and a local marketplace, the
        plugin cell covering only that route; `bin/install` after Gpub); a second, earlier kit for the real upgrade
        and rollback (decision 11), or they stay unobserved; the owner-account rows after the migration that installs
        the final candidate there; phases D and E on the phase-C installation; decision 15 in phase A; the
        `api-credentials` workaround for the no-identity option; the release commit's gate, the tag and
        `release-commit --tag`; the mounted-DMG run moved out of P2's owner column into steps 14 and 36.
      **Reconciliation** (lead, 26.10.2026): every row that cited the interim receipt (P2, F2, F7, F8, F11, F13–F18)
      checked against the merged harness (receipt v4 at fc3f3c8, merged in 3da4745: 48/48 synthetic, Inspector 4/4,
      Codex 9/9, Claude Code 9/9, 7 owner cells pending) and the seven final-audit cells (55/55 and Inspector 4/4 at
      2755559): every cited cell exists with the cited probe and status, and F15, F16 and F17 now cite Claude Code
      beside Codex. Under the status rules: F6 met (`search.boolean`, `search.filters`); F9 met with owner remainder
      (`documents.xlsx`, `documents.pptx`); F13 met with owner remainder (`prepare.styled_caption`,
      `prepare.reply_sources_058`, `refusal.text_options`); F18 met with owner remainder (the merged receipt,
      verified with both agent columns). The four items of 26.10.2026 are in their rows: 1b604f9 in P2 (open item 1
      closed), 12f53f6 in F13, 2c87e08 as open item 13; open items 2 and 9 closed; open item 12 added. The audit says
      that the 2755559 receipt was a scratch receipt and that the whole matrix with both agent columns runs again,
      with a kept receipt, on the build commit and in the final G1.
      Counts after the reconciliation: 11 met, 10 met with owner remainder, 2 partly met (P3 and F19: the lead's
      history report before decision 2, and the final G1, which follows the owner session), none not applicable or
      not met.
      **Closing pass** (26.10.2026, below): the lead's last review round and its check of the F18 fixes, fixed in
      four code and five docs commits. Counts after it: 10 met, 11 met with owner remainder (F12 joins), 2 partly met.
      Before the tick: the lead's check of this revision.
- [x] Mounted-DMG and `Install.command --prepare-only` run (final audit, open item 4; agent-side), 25.10.2026, on
      0c9f040 (0.59.0.dev0; the critic fixes after it change only `docs/plans/`). `release_build.py build --dry-run
      --offline` from the commit's `git archive` with no application identity (the index lists no
      `app-identity.json`), the pinned cached inputs and the natives `prepare_native.py` reproduces from the keg;
      `check` verified (9 assets, 11 kit files, publication `unreleased`, mode `adhoc-dry-run`). Hashes: DMG
      `49069de8e7f2588485f501e68ac49a210b92a76b0c976d72111ddb2d2652ca18` (ad hoc signature, `codesign --verify
      --strict` ok; a rebuild never reproduces the DMG's hash), index
      `1630d9c633de06a10f660218bcbdac74e1c5f7ba328f5218cb593fc90eee16f8`, archive
      `bfcb0e2f8cd7ce9edb439e96dc519d347d1b8f7058195cef50156de537081e7d` (81,979,248 bytes), seed `7fd33ab4…f547`
      (the 0.58.0rc1 gate's seed), `Install.command` `456fa664…9fc5`, kit `SHA256SUMS` `2cb6f4da…ddcc`,
      `release-build.json` `c6830d7b…7b4c`. The DMG was mounted read-only (`-nobrowse`) on a scratch directory
      outside `/tmp` and outside every live location; its files matched the kit's `SHA256SUMS`, and
      `Install.command --prepare-only` from the volume exited 0 with `verified: true`, target macos-arm64, 4,463
      files, 234,622,676 bytes unpacked and the index and archive hashes above. The staged release's own interpreter
      (`env -i`, `-I -S -B`) imported all 33 distributions and reported package 0.59.0.dev0, TDLib 1.8.67 at d1085f9
      (`getOption`, offline) and 64 tools matching the manifest's fingerprint. `bootstrap.sh` stages the release in
      its own `mktemp` directory under `/private/tmp` by design (it checks those anchors), so the staging could not
      be placed elsewhere; nothing was installed, no client configuration, Keychain item, LaunchAgent or live
      installation was touched, and the staging, the mount and the build output were removed afterwards. Needs the
      owner, so not done here: the signed build and everything after it, and Gatekeeper and quarantine on a
      browser-downloaded DMG (a locally built DMG carries no quarantine). The final candidate's build commit gets the
      same run before the owner's build (decision sheet, step 14 since the re-critic's revision; step 9 before it).
- [x] Open item for the criterion 5 audit (recorded 24.10.2026 from 0.55; not 0.55's): a first install never moves
      the default's own runtime.toml version line, so an uninstall and a reinstall at another version meets
      config_stale and then policy_conflict (0.53/0.54 behaviour; 0.55 left it unchanged under criterion 2).
      Carried into the audit as open item 1 (a first install rewrites that line only when it adopts a legacy
      service) and onto the decision sheet: fix it in the installer or disclose it.
      **Fixed in 1b604f9** (26.10.2026; lead decision on the decision sheet's option, owner review pending): by the rule
      the account-add repair uses for an alias's line (review fix 055d4), a first install over the default's kept
      session moves its `expected_package_version` line onto the installed release from an equal or older value,
      journaled (`policy_previous`, `policy`, by state on every run, the transaction's policy step now running for
      every first install), and refuses `policy_conflict` before the transaction for a newer value or a line it
      cannot rewrite exactly (one helper, `_refuse_newer_policy`, shared with the repair path). The adopted broker's
      pin still follows it at any version. A fresh install with no kept session, or over a line already naming the
      release, is byte-identical (no policy record, no session root; the criterion 2 journal goldens unchanged).
      Tests (`tests/test_bundle_kept_session.py`, 4 functions, failed first: the line was not moved, nothing refused,
      the resumed install did not move it): the move and its journal records, the installed release's policy
      accepted (`require_current`) and the next upgrade passing; a newer, a doubled and an unquoted line refused before
      the transaction with every watched file unchanged; equal and fresh installs untouched. Kill sweep scenario
      `first_install_over_a_kept_session` (153 kill points, a final check of the moved line), which fails on the old
      installer; the whole kill sweep and the seeded sequences pass (`49 passed, 104 subtests passed`). install.md
      (Uninstall) and reference.md (`policy_conflict`) say so. The audit's open item 1 and the decision sheet's row
      are for the lead to reconcile. Reconciled under criterion 5 (26.10.2026): open item 1 closed, the sheet's row
      in section 2 asks the owner to confirm the fix.
- [x] Open item for the criterion 5 audit (recorded 25.10.2026 by the F18 matrix; not fixed there):
      `retain_selection` of a message with any entity answers `selection_unavailable` whenever the entity carries
      TDLib's `"@type": "textEntity"` key, as TDLib's JSON writes every object, because
      `selection_projection.message_projection` accepts exactly `offset`, `length`, `type` (the selection tests'
      fixtures omit the key; the forward projection requires it). Observed on the synthetic account, where the
      same message forwards but cannot be retained; not yet checked on the real account. The matrix retains a
      message without entities and does not freeze this behaviour.
      **Fixed in 2c87e08** (26.10.2026): the projection accepts exactly TDLib's shape (the `@type` key with the value
      `textEntity`, nothing else beside `offset`, `length`, `type`); an entity without it, with another value or with
      an extra key still refuses. The sweep found no other reader of message entities with the mismatch:
      `forward_sources`, `edit_drafts.exact_styles` (edits, formatted-send and caption confirmations),
      `reply_formatted_sources` and `reply_media_sources` already require the key; the outgoing validators
      (`outgoing_formatting`, `tdjson`) read caller spans or our own native payloads. The three selection fixtures
      that omitted the key carry it, so their refusals are for the drift or the UTF-16 boundary. Tests (failed first,
      3): TDLib-shaped entities of five kinds and a caption project and every other shape refuses; through the broker
      a message with a hashtag and a bold span is retained, read by its selection, retained again to the same token
      and refused after its entities drift. Still not checked on the real account (the owner session). Reconciled
      under criterion 5 (26.10.2026): the audit's open item 13; the real check is the decision sheet's step 30.
- [x] Open item for the criterion 5 audit (recorded 25.10.2026 by the F18 matrix): a styled `prepare_text_send`
      under a policy without `formatted_text_send` answers `error` / `topic draft could not be prepared safely`
      instead of `capability_disabled` with `missing_capabilities`, as the reply tools do.
      **Fixed in 12f53f6** (26.10.2026; lead decision: the topic option too, since both are the request's options and
      the reply tools name both). `prepare_text_send` answers `status: capability_disabled`, `missing_capabilities`
      naming only what is missing in the reply tools' order (`send`, the styles, the topic), the fixed detail `text
      draft needs capabilities the owner has not enabled: missing_capabilities` and the derived `next_action:
      enable_capability`, before Telegram is read: at the broker (also for an option revoked while the draft is
      prepared; `config_stale` and provider failures keep `error`), at the proxy (its own policy check answers the
      same result and sends nothing on; it answered `draft broker is unavailable` for a disabled topic before) and
      through the official SDK (the guard still refuses the tool itself without `send`; the listed output schema
      admits the result). **Listed schema change** `tests/fixtures/schema_changes/0.59-text-send-capabilities.json`
      (`prepare_text_send` output: the status and the closed `missing_capabilities`); the fingerprint pin in
      `tests/test_account_registrations.py` and `MATRIX_SCHEMA_FINGERPRINT` move from `6a0b0971…7d` to
      `85b80a452c7643104688e8d45b282b235785c66217158b5b59e4c6ffd3abb071`. Tests
      (`tests/test_text_send_capability_results.py`: 14 failures, test functions and subtests, against the code before
      the fix, the cases of unchanged behaviour passing; two topic tests now expect the typed result); the matrix freezes it in `refusal.text_options` (below). The tool description is unchanged (the
      description budget had 4 characters left); reference.md says so.
- [x] **Matrix cells for F6, F9 and F13** (26.10.2026; the audit's agent-side gaps of F6, F9 and F13 and open item 9;
      spec [`final-consumer-matrix-spec.md`](final-consumer-matrix-spec.md), section 5a). 739468c and 2755559: seven
      synthetic cells (48 → 55) at frozen counts, all through the installed official SDK under the full policy unless
      named: `search.boolean` (6: all/any/none over the first chat's three messages, two branches deduplicated, local
      matching semantics, rehydrated matches), `search.filters` (7: sender, outgoing, incoming, forum topic with
      membership, topic + own sender + outgoing, the Boolean form in a topic), `documents.xlsx` (6: catalog with a
      hidden sheet, a range with an inline string, a number, a formula as data with its cached value and an empty
      cell), `documents.pptx` (5: catalog, a slide with its notes, a hidden slide without), `prepare.styled_caption`
      (4: a styled caption on a file, shown again by `get_draft`, and on a file reply), `prepare.reply_sources_058`
      (7: replies to a media caption with a text link (v24), a custom emoji (v25) and a voice note captioned with a
      media timestamp (v28), each with its payload in the bound evidence; the voice note goes beyond the work item's
      two sources because the audit's F13 gap names it, lead decision) and `refusal.text_options` (5, restricted).
      The synthetic account gains a captioned document and a voice note in chat 91002 and a forum supergroup outside
      the Main list; `searchChatMessages` keeps `sender_id` and `topic_id` as TDLib does; no earlier cell's rows and
      no agent cell change (the lead runs the agent columns with the whole matrix at the release candidate).
      `SURFACE_COUNTS` 157 → 183 structured results from all 64 tools. Unit tests for the fixtures failed first
      against the previous harness (5); the source sessions test passes every cell at its count.
      **Run** once at 2755559 (the harness after both commits; an earlier run at 739468c was interrupted with SIGTERM
      during its build, before the voice-note source was added: `interrupted`, exit 143, no temporary directory left)
      against the 0.58.0rc1 gate wheelhouse and toolhouse (`rc-0.58.0rc1-6d1680d`), Inspector 1.0.2 on Node 22.18.0
      from the same offline cache (lock sha256 `b7bcccd6fe10b8c9…`), synthetic and Inspector columns only:
      `consumer matrix passed: 55/55 synthetic cells; Inspector 4/4 cells; Codex unavailable (cli_not_given); Claude
      Code unavailable (cli_not_given); 7 owner cells pending`. Scratch receipt outside the repository (deleted
      afterwards, not kept as evidence), sha256 `8a563cc2b41de60638b95851ec9de1b4e7011ed334ce0cd101551dd43625914b`:
      wheel `605e4efd11848442061ed3601e996f69b7077259ab8d528b610d7575d08221f4` (2,201,391 bytes, 0.59.0.dev0), schema
      fingerprint `85b80a45…b071`, 64 tools, 183 structured results from all 64 tools, installer tomlkit 0.13.3 from
      the gate toolhouse, scripted approval. `check --commit 2755559e960c7961f51148f3ba9c8b510429ed80` with the same
      wheelhouse: `consumer matrix receipt verified: 55/55 synthetic cells; Inspector 4/4 cells; Codex unavailable
      (cli_not_given); Claude Code unavailable (cli_not_given); 7 owner cells pending (commit 2755559e…, wheel sha256
      605e4efd…21f4)`. The audit's F6, F9 and F13 rows and open item 9 are for the lead to reconcile. Reconciled
      under criterion 5 (26.10.2026): F6 met, F9 and F13 met with owner remainder, open item 9 closed.
- [x] **Closing pass** (26.10.2026; the lead's last review round, eleven confirmed findings, two code and nine docs,
      and its check of the F18 fixes, two code defects; both reports kept outside the public tree). Code fixes have
      a test that failed first against the commit before them.
      1. *A kept session's version line already naming the release refused `policy_conflict` when it carried trailing
         spaces or an inline comment* (low) — 733ef5b. `_policy_candidate` returns the file unchanged when its single
         version line already reads as the target release under TOML; only a line that must move needs the
         installer's exact form, for every caller. Test `test_a_line_naming_the_release_in_another_valid_form_changes_nothing`
         (`tests/test_bundle_kept_session.py`): both subtests failed first with `policy_conflict`; an older line with
         an inline comment still refuses before the transaction. The kill sweep, upgrade and account-change files
         pass. install.md and reference.md say so.
      2. *`update_draft` with styles its policy does not enable answered `draft is unavailable`* (low) — 1a8a39f
         (lead decision: fixed, since the tool description needs no change and so fits the description budget).
         The request's own style option, checked before the draft is looked up, answers `capability_disabled` with
         `missing_capabilities` (`send`, then `formatted_text_send` or `formatted_caption_send`), the fixed detail
         `draft revision needs capabilities the owner has not enabled: missing_capabilities` and `next_action:
         enable_capability`; `config_stale` and a stored draft's own state keep `unavailable`, as `get_draft`.
         **Listed schema change** `tests/fixtures/schema_changes/0.59-draft-revision-capabilities.json`
         (`update_draft` and `refresh_draft` outputs, one shared model); the fingerprint pin in
         `tests/test_account_registrations.py` and `MATRIX_SCHEMA_FINGERPRINT` move from `85b80a45…b071` to
         `8d8fc25c73c336b0ce32a8c965b59f57770b8aee9d971aacb0165d0162e00d28`; no matrix cell changes. Tests
         (`tests/test_draft_revision_capability_results.py`: 5 failures first, the model, the broker for a known and
         an unknown draft, the proxy and the official SDK; the unchanged cases passed); the 0.58 caption test that
         expected `unavailable` expects the typed refusal. reference.md and the matrix spec say so.
      3. *A first SIGTERM or SIGHUP during a normal matrix cleanup cut it short* (low; F18 fix check) — d4dd634. Every
         cleanup block runs whole under `deferred_interrupts` (the agent's kill and reap, the cell's broker stop and
         home removal, the column's write-back and scratch removal, the sessions' driver and broker stops and home
         removal, `run`'s and `check`'s work directory); a signal meanwhile raises afterwards. Test
         `test_a_first_signal_during_a_normal_cleanup_never_cuts_it_short`: a real harness process stalled inside the
         step gets one signal during the cell's broker stop and home removal, the sessions' broker stop and home
         removal and the Codex write-back; all five subtests failed first (a `tga.` home left twice, the second
         session broker alive, the `tgcm.` homes left, the spent refresh token kept). The matrix test file passes
         (`89 passed, 337 subtests passed`). The matrix spec (section 8) says so.
      4. *The harness docstring said nothing reaches the Keychain or a client configuration and gave the write-back
         condition incompletely* (low; F18 fix check) — 49ebc1e: Claude Code gets the caller's `HOME` and `USER` for its
         login through the default configuration location only; the write-back rewrites the caller's Codex
         `auth.json` only as `returned`, and leaves it on `kept`, `invalid` and `failed`.
      5. *Phase C's loopback route put the clean user on the build Mac* (medium) — 953a007: a separate clean Mac, the
         DMG served over the local network or sent by AirDrop (both quarantine it), the marketplace as a `git
         archive` unpacked with `ditto`, no git there; otherwise the machine bullet stays unmet and disclosed (P2,
         decisions 8 and 15).
      6. *F6, F9 and F13 rested on the deleted 2755559 scratch receipt* (medium) — 9cdcbf3: their status depends on the
         kept 55-cell receipt of the release candidate's matrix run (the lead runs it next, with both agent columns);
         a failing cell moves a row back to partly met.
      7. *Decisions 5 and 8 omitted that D and E run on the phase-C installation* (medium) — f37467d. Not signing:
         C, D and E cannot run, their remainders stay open and decision 11's upgrade route is blocked. Deferring C:
         D and E and their remainders defer with it; decision 11's upgrade route needs only the signed kit and still
         runs (stated as such rather than as blocked, since it does not use the phase-C installation), but its Keep
         option loses the installation steps 34–35 would use.
      8. *Decision 17's Complete silently included changing the native build method* (medium) — f37467d: Complete
         needs the source rebuild as the shipped method or a narrower, stated definition of the native-build item;
         decided with section 2's build method and decision 13.
      9. *Decision 13 had no step* (low) — f37467d: step 7 takes decisions 13 and 17 and the build method together,
         before step 14; the stricter reading means waiting for the rebuild spec or shipping with criterion 3 not met
         and disclosed.
      10. *Build-commit gate findings deferred to step 38* (low) — c5318ae: accepted, or the phase-A commit replaced,
          before step 15; step 38 keeps only the final G1's.
      11. *F12 met with no remainder after its confirmations changed* (low) — 9cdcbf3: F12 names the 057, 057b and 0.58
          confirmation changes and is met with owner remainder (sheet steps 25, 27 and 29).
      12. *Option B's consequences omitted the re-keyed evidence chain* (low) — c5318ae: an old-to-new commit id map
          with the release record; the shipped notes' and the audit's and task records' citations updated in the
          build commit.
      13. *Step 14's critic review had no stated range* (low) — c5318ae: `v0.58.0rc1`..build commit, earlier ranges
          covered by each candidate's review; the release commit's gate reuses it.
      The audit's and the sheet's status lines, P2, F11, open item 1 and section 2 record the two code decisions
      (items 1 and 2) in the commit that adds this block (b0e3d2d).
      **Suite** at b0e3d2d (isolated HOME): `4299 passed, 23429 subtests passed in 1552.74s`, 0 failed, 0 skipped; no
      disk-exhaustion failure, nothing rerun.
      **Matrix**, since the harness and the schema changed: the synthetic and Inspector columns once at b0e3d2d against
      the 0.58.0rc1 gate wheelhouse and toolhouse (`rc-0.58.0rc1-6d1680d`), Inspector 1.0.2 on Node 22.18.0 from the
      same offline cache, scripted approval: `consumer matrix passed: 55/55 synthetic cells; Inspector 4/4 cells; Codex
      unavailable (cli_not_given); Claude Code unavailable (cli_not_given); 7 owner cells pending`. Scratch receipt
      outside the repository (deleted afterwards, not kept as evidence), sha256
      `d00ae772f3cd273c23cb2b6beebc4ccb604f475281dbaafb2f0d9d9dbc0e176a`: wheel
      `4b508746c675ad460cebdcf7e54e97ff74baee9759789f1544345c2587a18d67` (2,202,327 bytes, 0.59.0.dev0), schema
      fingerprint `8d8fc25c…0d28`, 183 structured results from all 64 tools, installer tomlkit 0.13.3 from the gate
      toolhouse. `check --commit b0e3d2d52e4a23875e4cd0267617920d226e8b82` with the same wheelhouse: `consumer matrix
      receipt verified: 55/55 synthetic cells; Inspector 4/4 cells; Codex unavailable (cli_not_given); Claude Code
      unavailable (cli_not_given); 7 owner cells pending (commit b0e3d2d5…, wheel sha256 4b508746…8d67)`. The kept
      55-cell receipt with both agent columns is the lead's next run (F6, F9 and F13 depend on it).
- [ ] **Owner row** — deferred to the owner session (criterion 6): Owner session rows (clean user, dialogs, two accounts, Claude Desktop, migration) and Gpub.

---

## Stop log

| # | hypothesis | what I did | what I saw |
|---|---|---|---|
| 1 | F18 part 1 full suite at 754e759 is green | isolated-HOME suite, 25.10 | 16 failed: 15 `No space left on device` (the host disk reached 0 bytes free, other projects) in prepare_payload, release_package, release_pins and repin_native, all passing on rerun; 1 real: `test_product_identity` caught the bundle cells' server-name literal, fixed in 4be1e95 |
| 2 | the same at 4be1e95 | isolated-HOME suite, 25.10 | `1 failed, 4208 passed, 23171 subtests passed`, 0 skipped; the failure was `test_bundle_kill_sweep` (`OSError(28)` during a low-disk window), passing alone (`1 passed`) |
| 3 | `test_install_guide_recipe` hangs | sampled the stuck processes during the first suite run (76e04d4, which then ended in disk errors) | a pasted `bash --norc -i` and its writer deadlocked on pipes (the test's 60 s timeout cannot fire while `communicate` is blocked in a write); the next two runs passed it; not caused by F18 |
| 4 | the recipe run can be made bounded in every case without weakening its checks | ac09c2b: a reproducer runs `run_shell` in a child process under a 45 s guard with a selector that reports the stdin pipe writable while input is left (the sampled host) | on the old run both cases hung past the guard (an interactive bash blocked on its prompts, a shell that never reads): `2 failed` in 90 s; with the shell in its own session, output to files, the paste from a thread and the process group killed at the deadline both pass (`4 passed, 90 subtests passed` for the file) |
| 5 | F18 part 2 full suite at 45a118e is green | isolated-HOME suite, 25.10, after the matrix run | `4233 passed, 23267 subtests passed in 1627.10s`, 0 failed, 0 skipped (log `matrix-final/suite-45a118e.log`) |
| 6 | the F18 review fixes leave the full suite green | isolated-HOME suite at 55c970c (the receipt commit fc3f3c8 plus the tasks record), 25.10–26.10, after the matrix run | `4244 passed, 23301 subtests passed in 1765.15s`, 0 failed, 0 skipped (log `matrix-final/suite-55c970c.log`) |
| 7 | The dry-run build accepts any owner-only output directory outside the checkout and `/tmp` | Built with the output under the temporary directory's `/var` spelling | Refused `assemble_failed`: the bundle producer refuses a symlinked ancestor, and `/var` is a symlink. Rebuilt under the resolved `/private/var` spelling: built and verified. By design; no change |
| 8 | the four final-acceptance items (2c87e08, 12f53f6, 1b604f9, 739468c, 2755559) leave the full suite green | isolated-HOME suite at bf09630 (the matrix run and the task record included), 26.10 | `4291 passed, 23412 subtests passed in 1650.04s`, 0 failed, 0 skipped; no disk-exhaustion failure, nothing rerun |
| 9 | the criterion 5 reconciliation and the re-critic's fixes (docs only) leave the full suite green | isolated-HOME suite at ab82e9a (audit, decision sheet and task record committed), 26.10; the privacy guard and the anchor test before each docs commit | `4291 passed, 23412 subtests passed in 1654.11s`, 0 failed, 0 skipped; privacy and anchors `8 passed, 55 subtests passed`; the commit recording this changes only this file and one sentence of the decision sheet |
| 10 | the closing pass (four code commits, the docs and the task record) leaves the full suite green | isolated-HOME suite at b0e3d2d, 26.10; the privacy guard and the anchor test before each docs commit; then the synthetic and Inspector matrix columns at b0e3d2d | `4299 passed, 23429 subtests passed in 1552.74s`, 0 failed, 0 skipped, nothing rerun; matrix 55/55 and Inspector 4/4, `check` verified (scratch receipt); the commit recording this changes only this file and the wrapping of two audit lines |
