# Final acceptance — tasks

Spec: [`final-acceptance-spec.md`](final-acceptance-spec.md).

- [ ] Criterion 1: Full isolated-HOME suite green with 0 skipped (suite).
- [ ] Criterion 2: Every synthetic and agent matrix cell PASS with a bound receipt; a deliberate schema change fails it (harness).
- [x] Criterion 3: TDLib procedure written; rehearsal reproduces d1085f9 natives and lock byte-identically (rehearsal record).
      The procedure is [`docs/tdlib-upgrade.md`](../tdlib-upgrade.md); the rebuild route is
      `packaging/build_tdlib_source.py` (c14e37d, 4fffce5). The [rehearsal record](final-tdlib-rehearsal.md)
      shows two clean builds at two build paths, 25.10.2026: the natives (libtdjson
      `46e04c99…60a7`) and the lock-shaped document (`72b8e0cf…33cf`) are byte-identical, and every output
      is equal. `prepare_native.py` still reproduces the shipped lock (`489b8ea9…c1ab`). The source-built
      libtdjson differs from the Homebrew-derived pin, which is recorded as information. Not exercised,
      because there is no candidate and no trigger: steps 1 and 5–7 on a new commit, including the owner's
      real read-only check.
- [x] Criterion 4: F19 transport pins pass (unit tests) — `tests/test_transport_scope.py`, 823ccf7.
- [ ] Criterion 5: Audit table complete; critic finds no unresolved applicable gap (critic report).
- [ ] Open item for the criterion 5 audit (recorded 24.10.2026 from 0.55; not 0.55's): a first install never moves
      the default's own runtime.toml version line, so an uninstall and a reinstall at another version meets
      config_stale and then policy_conflict (0.53/0.54 behaviour; 0.55 left it unchanged under criterion 2).
- [ ] **Owner row** — deferred to the owner session (criterion 6): Owner session rows (clean user, dialogs, two accounts, Claude Desktop, migration) and Gpub.

---

## Stop log

| # | hypothesis | what I did | what I saw |
|---|---|---|---|
| | | | |
