# Final acceptance — tasks

Spec: [`final-acceptance-spec.md`](final-acceptance-spec.md).

- [ ] Criterion 1: Full isolated-HOME suite green with 0 skipped (suite).
- [x] Criterion 2: Every synthetic and agent matrix cell PASS with a bound receipt; a deliberate schema change fails it (harness).
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
- [ ] Criterion 3: TDLib procedure written; rehearsal reproduces d1085f9 natives and lock byte-identically (rehearsal record).
- [x] Criterion 4: F19 transport pins pass (unit tests) — `tests/test_transport_scope.py`, 823ccf7.
- [ ] Criterion 5: Audit table complete; critic finds no unresolved applicable gap (critic report).
- [ ] Open item for the criterion 5 audit (recorded 24.10.2026 from 0.55; not 0.55's): a first install never moves
      the default's own runtime.toml version line, so an uninstall and a reinstall at another version meets
      config_stale and then policy_conflict (0.53/0.54 behaviour; 0.55 left it unchanged under criterion 2).
- [ ] Open item for the criterion 5 audit (recorded 25.10.2026 by the F18 matrix; not fixed there):
      `retain_selection` of a message with any entity answers `selection_unavailable` whenever the entity carries
      TDLib's `"@type": "textEntity"` key, as TDLib's JSON writes every object, because
      `selection_projection.message_projection` accepts exactly `offset`, `length`, `type` (the selection tests'
      fixtures omit the key; the forward projection requires it). Observed on the synthetic account, where the
      same message forwards but cannot be retained; not yet checked on the real account. The matrix retains a
      message without entities and does not freeze this behaviour.
- [ ] Open item for the criterion 5 audit (recorded 25.10.2026 by the F18 matrix): a styled `prepare_text_send`
      under a policy without `formatted_text_send` answers `error` / `topic draft could not be prepared safely`
      instead of `capability_disabled` with `missing_capabilities`, as the reply tools do.
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
