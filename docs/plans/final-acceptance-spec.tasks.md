# Final acceptance — tasks

Spec: [`final-acceptance-spec.md`](final-acceptance-spec.md).

- [ ] Criterion 1: Full isolated-HOME suite green with 0 skipped (suite).
- [ ] Criterion 2: Every synthetic and agent matrix cell PASS with a bound receipt; a deliberate schema change fails it (harness).
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
      Before the tick: the lead reconciles the rows that cite the interim F18 receipt (P2, F2, F7, F8, F11,
      F13–F18) with the merged receipt, and the fixes get an independent check.
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
      same run before the owner's build (decision sheet, step 9).
- [ ] Open item for the criterion 5 audit (recorded 24.10.2026 from 0.55; not 0.55's): a first install never moves
      the default's own runtime.toml version line, so an uninstall and a reinstall at another version meets
      config_stale and then policy_conflict (0.53/0.54 behaviour; 0.55 left it unchanged under criterion 2).
      Carried into the audit as open item 1 (a first install rewrites that line only when it adopts a legacy
      service) and onto the decision sheet: fix it in the installer or disclose it.
- [ ] **Owner row** — deferred to the owner session (criterion 6): Owner session rows (clean user, dialogs, two accounts, Claude Desktop, migration) and Gpub.

---

## Stop log

| # | hypothesis | what I did | what I saw |
|---|---|---|---|
| 1 | The dry-run build accepts any owner-only output directory outside the checkout and `/tmp` | Built with the output under the temporary directory's `/var` spelling | Refused `assemble_failed`: the bundle producer refuses a symlinked ancestor, and `/var` is a symlink. Rebuilt under the resolved `/private/var` spelling: built and verified. By design; no change |
