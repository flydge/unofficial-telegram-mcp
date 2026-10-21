# Final acceptance — F18 matrix, TDLib procedure, F19, audit — spec

**Status:** lead spec, 21.10.2026, written under the owner's standing delegation. Every decision below is a
**lead decision, owner review pending** (see the [completion plan](completion-plan.md)).
**Branch:** `final-acceptance`, from the 0.58 release candidate.
**Tasks:** [`final-acceptance-spec.tasks.md`](final-acceptance-spec.tasks.md)

## 1. One sentence

The last candidate is accepted when:
- every applicable cell of the installed consumer matrix has evidence;
- a TDLib upgrade can be done by a written, rehearsed procedure;
- STDIO-only transport is pinned;
- a requirement-by-requirement audit of P0–P3 and F1–F19 finds no open applicable gap;
- the owner session closes the rows only the owner can produce.

## 2. Work items

- **F18 consumer matrix.**
  - The 0.54 matrix spec and harness are extended to every tool added through 0.58.
  - Columns:
    - official SDK;
    - MCP Inspector, scripted CLI mode;
    - Codex, real `codex exec`;
    - Claude Code, real `claude -p` when logged in;
    - Claude Desktop, the owner session.
  - Rows: census and fingerprint, output schemas, every refusal text, image blocks, cursors, recovery, caps,
    every handshake revision, approval denial by dialog timeout, and the upgrade, rollback and uninstall
    paths of the installed bundle.
  - Agent cells run against a synthetic broker from a `git archive` wheel. The receipt is bound to the
    commit, the wheel hash and the fingerprint.
- **Controlled TDLib upgrade procedure** (`docs/tdlib-upgrade.md`). Triggers, then the steps in order:
  1. Pin a candidate commit.
  2. Build it reproducibly with `prepare_native.py`.
  3. Diff `td_api.tl` against every allowlisted request and update, with a tested allowlist diff.
  4. Re-run the media schema pins.
  5. Re-sign and repin with `repin_native.py` under a new `APPROVED_PINS` row.
  6. Run the full suite and the matrix.
  7. Do a real read-only check on the owner's account.
  8. Keep a rollback path to the old pin.

  The rehearsal rebuilds the current pin (d1085f9) from source twice through the procedure and shows
  byte-identical natives and lock. No upgrade happens without a concrete trigger.
- **F19.** Streamable HTTP is not applicable: there is no demonstrated cloud consumer.
  `tests/test_transport_scope.py` pins STDIO-only transport, the Unix-socket broker and the loopback-only
  connect screen.
- **Final audit.** One table row per requirement (P0–P3, F1–F19). Each row records its status, its evidence
  (commit, test, real record) and any owner-only remainder. An independent critic reviews the table
  against the code.
- **Release notes and roadmap** for the final version, and the consolidated owner decision sheet.

## 3. Acceptance criteria

| # | criterion | how verified |
| --- | --- | --- |
| 1 | Full isolated-HOME suite green with 0 skipped | suite |
| 2 | Every synthetic and agent matrix cell PASS with a bound receipt; a deliberate schema change fails it | harness |
| 3 | TDLib procedure written; rehearsal reproduces d1085f9 natives and lock byte-identically | rehearsal record |
| 4 | F19 transport pins pass | unit tests |
| 5 | Audit table complete; critic finds no unresolved applicable gap | critic report |
| 6 | Owner session rows (clean user, dialogs, two accounts, Claude Desktop, migration) and Gpub | **owner session** |

## Result

(Filled at the end.)
