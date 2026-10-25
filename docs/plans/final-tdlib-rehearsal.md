# Final acceptance — TDLib procedure rehearsal (criterion 3)

Spec: [`final-acceptance-spec.md`](final-acceptance-spec.md), criterion 3. Procedure: [`docs/tdlib-upgrade.md`](../tdlib-upgrade.md).
Date: 25.10.2026, on the build Mac. This is a lead record. The evidence (every output, both build logs, the
summaries, the comparison reports and `SHA256SUMS`) is kept in the owner's private gate directory, outside
the public tree.

## What was rehearsed

Step 2 of the procedure (build reproducibly) was run on the current pin, d1085f9, with
`packaging/build_tdlib_source.py` at commit 4fffce5. There were two builds from clean build trees:

- **Build A:** a 50-character build root.
- **Build B:** a different directory 29 characters longer and one level deeper.

Each build removed its tree after recording its outputs, and the second started only afterwards. Steps 3
and 4 ran on the same pin, and `prepare_native.py` was run again to confirm that the shipped lock still
reproduces. No upgrade was made: there is no trigger.

## Inputs

- **Source:** the codeload archive of `d1085f9cebc5a62379991ae1652673954f229c1f`, checked against
  `packaging/payload-lock.json`. It has 5,819,665 bytes, sha256
  `ba44c6eedc321b50082045167554e4a01c01f321cedd54c3cccc2beff8e4c78d`, 1675 regular files and 29,629,794
  unpacked bytes. Its pax comment names the commit, and every member has mtime 1787589927, which became
  `SOURCE_DATE_EPOCH`.
- **Exported tree:** the exported source tree that `prepare_native.py` reads holds the same 1675 paths with
  identical contents. Its 13 example scripts lack their executable bit; the build does not use them.
- **Schema and version:** `td_api.tl` has sha256
  `326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87`, equal to `APPROVED_PINS` row 0.
  `CMakeLists.txt` names 1.8.67.
- **OpenSSL:** the pinned keg `openssl@3` 3.6.4.

## Toolchain

| Tool | Version |
| --- | --- |
| macOS | 26.6.2 (25G83), arm64 |
| Xcode | 27.1 (27A9275), selected developer directory |
| clang | Apple clang 21.0.0 (clang-2100.3.34.2) |
| ld | ld-27037.1 |
| SDK | macOS 27.0 |
| Command Line Tools | 26.6.0.0.1781586589 |
| CMake | 4.4.3 |
| gperf | GNU gperf 3.3 |
| make | GNU Make 3.81 |
| Python (tool runner) | 3.14.6 |

## Result

**Byte-identical.** `build_tdlib_source.py compare` of build A against build B exits 0: every one of the 11
files and 8 directories has the same name, mode and bytes. Both builds passed `validate_native` with the
`getOption` probe, which answered version 1.8.67 and commit d1085f9, and both removed their build trees.

| Output | Bytes | sha256 |
| --- | --- | --- |
| `native/libtdjson.dylib` | 24,183,984 | `46e04c99f5de44890b788ec034ae3f341d196246cb815f13be4106a00d4e60a7` |
| `native/libssl.3.dylib` | 901,744 | `7e89651576fc4dfe9461f65de448fc99990dc5c32c06c413d86bdbbfc11e9774` |
| `native/libcrypto.3.dylib` | 4,862,416 | `f9563883fdbb4f2b367c708a6abd786119c935d3f9cd01fa8e11c34666775d1b` |
| `source-rebuild-lock.json` (the lock-shaped document) | 3,033 | `72b8e0cf6626460a73d290ce2ab382d543fe41fce8dcccc3ef06f417569133cf` |
| `include/td/telegram/td_api.h` | 1,469,271 | `8285128040cace9611f84abe6ff517bb0a14d35004a557a78c3cf001c58ebae5` |
| `provenance/source-build.json` | 1,658 | `06932eb8677bd71e1c00038a75ce3a33dbcb6caaea9839abfca328168f1aa9e7` |
| `provenance/openssl-build.json` | 1,464 | `dd73f49f535bf0fcec0bdf8044712e04ff18d24e1f00dbd33b133bc3cc9dda26` |
| `notices/tdlib/LICENSE_1_0.txt` | 1,338 | `c9bff75738922193e67fa726fa225535870d2aa1059f91452c411736284ad566` |
| `notices/openssl-3.6.4/LICENSE.txt`, `AUTHORS.md`, `README.md` | 10,175, 1,022, 8,519 | equal to the `native_notices` hashes in `packaging/payload-lock.json` |

A third clean build was made at a third path (56 characters) with the final tool, 76d6bc5. That commit
changes, after 4fffce5, only how the summary reads the committed lock and how a tree left behind is
reported, and its outputs are identical too (`compare` exits 0). Two earlier builds in build A's path were
also identical: one from the working tree before c14e37d, which differed only in a path-grammar check, and
one from c14e37d. They were made while the tool was being finished. With the paths normalised, the two build logs differ only in timings and in the order
of parallel jobs. In the rebuilt `libtdjson`, 558 strings carry the mapped prefix `/tdlib-rebuild/`, and
none contains the build root or a home-directory path.

No nondeterminism was found, so the stop log stays empty. These settings made the result path-independent:

- the prefix maps;
- a fixed install-name directory (`@loader_path`);
- `CMAKE_SKIP_RPATH`;
- building from a fresh extraction, because TDLib writes its generated sources into the source tree;
- a Release build without debug information, so the linker writes no object-file paths or times.

## Steps 3 and 4 on the pin

- **Step 3:** `build_tdlib_source.py requests` of `td_api.tl` against itself reports 35 allowlisted requests:
  34 unchanged and 1 absent, `checkDatabaseEncryptionKey`. Its state, `authorizationStateWaitEncryptionKey`,
  is not in the pinned schema, so the request is never sent. Removing it is left to a separate reviewed
  allowlist diff and is not part of this rehearsal.
- **Step 4:** the media schema pins pass against the rebuilt header through `TGMCP_TDLIB_HEADER`:
  `tests/test_tdlib_media_schema.py` and `tests/test_album_send.py`, 80 passed. The rebuilt header is byte
  for byte the Homebrew keg's `include/td/telegram/td_api.h`.

## The shipped, Homebrew-derived lock (information, not the criterion)

- `prepare_native.py`, after the helper refactor of d6d84bd, still reproduces the committed
  `packaging/tdlib-lock.json` byte for byte from the pinned kegs, with the probe (lock sha256
  `489b8ea9c0f99172a198afef2dad8f8b1cb9df3c7070df8720d982e577dbc1ab`, `differing_fields: []`).
- The rebuilt `libssl.3.dylib` and `libcrypto.3.dylib` equal the committed ones. They are the same keg
  files, with the same relocation and ad-hoc signature.
- The rebuilt `libtdjson.dylib` does **not** equal the committed one: 24,183,984 bytes against 17,638,336,
  and a `__text` section of 21.6 MB against 15.3 MB. The known differences:
  - **Toolchain.** The lock records Xcode 26.6 and its libtdjson names SDK 26.5; the rebuild used Xcode 27.1
    and SDK 27.0.
  - **Implicit flags.** Homebrew's flags (`std_cmake_args` and its compiler shims) were not captured: the
    lock says `homebrew-std_cmake_args-not-captured`. The larger code is consistent with an optimisation
    level other than CMake's Release `-O3`, but the receipt cannot confirm that.
  - **The rebuild's own fixed flags.** `-headerpad_max_install_names`, the install-name directory and the
    prefix maps. Homebrew's binary embeds relative source paths (`td/telegram/…`); the rebuild's carry
    `/tdlib-rebuild/source/`.

  In `source-rebuild-lock.json`, the fields that differ from the committed lock are the libtdjson bytes and
  sha256, plus `build.method`, `build_os`, `xcode`, `known_formula_flags` and `implicit_flags`. The shipped
  lock stays `homebrew-receipt-relocated` with `reproducibility: unverified`. The rebuild document is refused
  by the v1 grammar and never replaces it.

## Criterion 3: what is shown, and the reading the owner confirms

- **Shown:** independent clean rebuilds of d1085f9 from its pinned source archive are byte-identical to each
  other: the natives (libtdjson `46e04c99…60a7`, 24,183,984 bytes), the lock-shaped document
  (`72b8e0cf…33cf`) and every other output, at three build paths, plus the two earlier builds.
- **Shown:** the shipped, Homebrew-derived pin (libtdjson `65ef0643…a12c`, 17,638,336 bytes; lock
  `489b8ea9…c1ab`) is reproduced byte for byte by `prepare_native.py` from the installed keg. That is a
  re-relocation of the same keg, not a rebuild.
- **Not shown:** a rebuild from source that reproduces the shipped natives or the shipped lock.

The spec's work item says the rehearsal "rebuilds the current pin from source twice … and shows
byte-identical natives and lock". Criterion 3 is marked met in that reading: the rebuilds are identical
to each other. Its table row ("reproduces d1085f9 natives and lock byte-identically") can also be read
as the shipped natives, and this rehearsal does not meet that reading. The owner confirms the reading
(item 13 of the [owner decision sheet](completion-plan.md#owner-decision-sheet-prepared-for-the-end)).

## What this does not show

- **No candidate.** There is no candidate commit, so steps 1, 5, 6 and 7 were not exercised on one: pinning
  a new archive, re-signing and repinning under a new row, the suite and matrix with a candidate linked, and
  the owner's real read-only check. Step 7 is an owner row.
- **One toolchain.** Reproducibility is shown on one host and one toolchain. Another Xcode gives other bytes;
  the toolchain is part of the provenance, and a comparison is meaningful only between builds that record
  the same one.
- **Shipped natives not rebuilt.** The shipped natives still come from the Homebrew keg. Making the source
  rebuild the shipped method would be a reviewed grammar change (the v1 build method literal) and an owner
  decision; it is not proposed here.

## Review fixes (25.10.2026)

An independent review of the procedure and this record found two medium and eight low points. Each is fixed
as follows; the tool commits after 76d6bc5 are 48600cc, 4783c0b and 0c4d9b8. None of them changes what a
build writes: the archive check moved earlier, the committed-lock comparison is a summary field, the
signal handling only ends the step's process group, and row 0 is the only row today. So the outputs
above stand, and no build was repeated.

1. **Rollback across a session-database migration (medium).** Steps 7 and 8 pointed to a session backup
   that the security rollback section never described, and the launcher's rollback guard hashes only
   `enrollment.json` and `runtime.toml`, so it would not notice a migrated `tdlib/db`. The owner procedure
   is now written in step 7 (save) and step 8 (restore) and in
   [TDLib session backup and restore](../security.md#tdlib-session-backup-and-restore). Saving stops each
   broker with `launchctl bootout`, then copies each account's `tdlib/` to an owner-only
   `tdlib-backup-<date>` next to it without changing the original, and records the `key_account` the
   enrollment names. The Keychain key is not copied; the copy opens with it. Restoring stops the broker
   before the rollback, so the old TDLib never opens the migrated database, then rolls the release back,
   moves `db` and `files` aside (never deleted) and copies the saved ones back into the same `tdlib/`,
   whose inode the enrollment binds. Then it starts the broker and runs `connect`. Both documents, and the
   install guide's rollback, state that a launcher rollback alone does not undo a database migration.
   **Decision: no installer guard now.** A hash of `tdlib/db` changes whenever TDLib runs, the database
   version is readable only with the Keychain key, which the installer never reads, and a guard on the
   releases' TDLib pins could not fire before a pin change. It must ship in the launcher of the first
   release that changes the pin, so that upgrade's spec decides it.
2. **The OpenSSL pin sites (medium).** "Where the pin lives" now lists `native_notices` in
   `packaging/payload-lock.json` (the version and the notice hashes that `prepare_payload.native_tree`
   checks against the lock), `BuildProvenance.openssl_version`, and the wording in `docs/licensing.md` and
   `docs/third-party-notices.md`. The search is `git grep -n -F -e d1085f9 -e 1.8.67 -e 3.6.4`. Step 1
   describes an OpenSSL-only advisory, and step 2 refills `native_notices` and keeps the old natives, since
   the replaced TDLib keg has the same name. An OpenSSL-only candidate row names the same commit as the
   kept row, which `build_tdlib_source.py` refused (exactly one row of the commit). It now builds row 0,
   as `prepare_native.py` does (4783c0b).
3. **The kept `APPROVED_PINS` row (low).** Step 1 now says to rewrite the kept row's commit, schema sha256
   and license URL as literals before `COMMIT` and `SCHEMA_SHA256` move. It gives a check that the rows
   stay distinct.
4. **The committed-lock comparison (low).** A committed lock that the v1 grammar no longer reads, or a
   missing one, is reported as `"committed_lock": "unreadable"` after a complete build instead of refusing
   (4783c0b).
5. **An interrupt left cmake and make running (low).** The step's process group is killed and reaped
   whenever the wait ends other than by the step's exit. SIGTERM and SIGHUP are caught for the step and
   refused as `build_interrupted`. A test signals a real tool process whose child group would outlive it
   (48600cc).
6. **An empty `TGMCP_TDLIB_HEADER` (low).** An empty value now fails like a missing file instead of reading
   the linked keg's header (0c4d9b8), and step 4 says so.
7. **The archive check (low).** `build` checks the archive's length and sha256 before it creates the log,
   the build root or the output; a test shows that a mismatch leaves nothing behind (4783c0b).
8. **Doc gaps (low).** Step 8 says to relink the kept old kegs after a revert, since `EXPECTED_TDLIB_KEG` and
   the schema pins read the linked keg. Step 2 gives the commands that extract a source tree for
   `prepare_native.py --tdlib-source` and for `requests --old/--new`.
9. **Evidence format (low).** In the private gate directory, `SHA256SUMS` now has the standard
   `sha256  name` lines (`shasum -a 256 -c SHA256SUMS`: 88 OK), with the byte counts in
   `SHA256SUMS.bytes`. All 88 entries were checked against the files before the rewrite, and the listing
   still equals the files present. `run-a.status` and `run-b.status`, which were empty, now hold `exit=0`
   from `run-ab.exit`, the one exit status of the chained A and B builds.
10. **The criterion 3 reading (low).** Stated above, and added to the owner decision sheet.
