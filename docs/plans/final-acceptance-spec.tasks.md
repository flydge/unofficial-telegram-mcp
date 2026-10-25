# Final acceptance — tasks

Spec: [`final-acceptance-spec.md`](final-acceptance-spec.md).

- [ ] Criterion 1: Full isolated-HOME suite green with 0 skipped (suite).
- [x] Criterion 2: Every synthetic and agent matrix cell PASS with a bound receipt; a deliberate schema change fails it (harness).
      Met for every applicable cell (25.10.2026, part 2 below): 48/48 synthetic, Inspector 4/4 and Codex 9/9 in one
      receipt verified by `check`; the Claude Code column is not applicable on this machine (`claude -p` is not
      logged in inside an isolated configuration directory, recorded `unavailable` / `not_logged_in`) and becomes
      applicable when an operator exports a Claude credential for a run. Lead decision, owner review pending.
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
        still fail run and receipt check as required, a receipt with passed agent cells included.
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
