# Controlled TDLib upgrade

[Overview](../README.md) · [Releasing](releasing.md) · [Security and rollback](security.md#rollback) · [Roadmap](roadmap.md)

The TDLib pin (source commit, library version, API schema, native libraries and their lock) changes only
through this procedure and only for one of the triggers below. A newer TDLib release on its own is not a
trigger. Every command names a file that exists in this repository; run them from the root of a clean
checkout, on a branch of its own, with the development environment from `CLAUDE.md` (`.venv/bin/python`).
Angle brackets stand for private paths on the build Mac. Keep them outside the repository and outside the
temp roots the OS clears (`/tmp`, `/var/folders` and their `/private` forms). The procedure was rehearsed on
the current pin; see the [rehearsal record](plans/final-tdlib-rehearsal.md).

## The current pin

| Item | Value |
| --- | --- |
| Source commit | `d1085f9cebc5a62379991ae1652673954f229c1f` (TDLib `1.8.67`) |
| `td_api.tl` sha256 | `326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87` |
| Source archive | codeload tarball of that commit, sha256 `ba44c6eedc321b50082045167554e4a01c01f321cedd54c3cccc2beff8e4c78d`, 1675 files |
| ABI | legacy `td_json_client_*` |
| Minimum macOS | 26.0, arm64 |
| OpenSSL | 3.6.4 (the Homebrew `openssl@3` keg) |
| Native build method | `homebrew-receipt-relocated`: the Homebrew keg `HEAD-d1085f9`, relocated and ad-hoc signed by `packaging/prepare_native.py` |

## Triggers

An upgrade starts only for one of these reasons, and its spec names the reason (see "Where things go" in
`CLAUDE.md`):

1. **Security advisory.** A published advisory affects the pinned TDLib or the pinned OpenSSL. An
   OpenSSL-only advisory changes `openssl_version` and the OpenSSL keg, and still runs every step.
2. **Telegram API change.** A Telegram API change breaks a request this product uses. Examples: an
   allowlisted request starts failing, the provider refuses the pinned layer, or a reader needs a
   constructor that the pinned schema does not have.
3. **Platform or toolchain change.** A macOS or Xcode release cannot run or build the pinned natives. The
   minimum OS is part of the pin.
4. **Owner request.** The owner asks for the upgrade.

## Where the pin lives

| File | What it pins |
| --- | --- |
| `src/telegram_search_mcp/native_runtime.py` | `COMMIT`, `SCHEMA_SHA256` and `APPROVED_PINS`. Each pin is one reviewed row: commit, version, schema sha256, minimum OS, ABI, license URL, OpenSSL version, build method, the unsigned artifacts (name, bytes, sha256) and the approved Developer ID teams. The v1 lock grammar (`NativeManifest`, `BuildProvenance`, `NativeNotices`) admits exactly one pin. Row 0 is the pin `prepare_native.py` builds. |
| `packaging/tdlib-lock.json` | The v1 lock of the unsigned natives. Only `prepare_native.py` writes it; it is never edited by hand. |
| `packaging/payload-lock.json` (`tdlib_source`) | The source archive: commit, URL, bytes, sha256, member count and unpacked size. The bundle ships it as corresponding source, and `build_tdlib_source.py` builds from it. |
| `packaging/prepare_native.py` | `TDLIB_KEG`, `OPENSSL_KEG` and `OPENSSL_OPT`: the kegs the shipped natives come from. |
| `src/telegram_search_mcp/config.py` | `EXPECTED_TDLIB_KEG`, `EXPECTED_TDLIB_VERSION` and `EXPECTED_TDLIB_COMMIT`, which a Homebrew or source installation checks with the `getOption` probe. |
| `src/telegram_search_mcp/tdjson.py` | `_ALLOWED_REQUEST_TYPES` and the narrower sets (`_CHANGE_REQUEST_TYPES`, `_DELETE_REQUEST_TYPES`, `_CALL_REFUSED_TYPES`, `_AUTHORIZATION_CONTROL_TYPES`). They are frozen by `tests/test_caption_edit.py` (`ALLOWED`) and `tests/test_reply_families_058.py` (`ALLOWED_0_57`). |
| `tests/test_tdlib_media_schema.py`, `tests/test_album_send.py` | The schema pins: the exact request objects the client builds, compared with the generated `td_api.h` through `tests/tdlib_header.py`. |
| Notes tied to the pin | Behaviour that was verified against the pinned sources, such as `src/telegram_search_mcp/auth_transport.py`, `outgoing_drafts.py`, `outgoing_formatting.py`, `send_observations.py` and `bundle_runtime.py`, plus the wording in `docs/install.md`, `docs/reference.md`, `docs/security.md` and `docs/licensing.md`. `git grep -n -e d1085f9 -e 1.8.67` lists them all. |

## Procedure

### 1. Pin a candidate commit

Fetch the codeload archive of the candidate commit `<commit>` into a private cache. Then read every pin
value from the verified archive:

```bash
curl -fsSL -o <cache>/td-<commit>.tar.gz https://codeload.github.com/tdlib/td/tar.gz/<commit>
PYTHONPATH=src .venv/bin/python packaging/build_tdlib_source.py facts --source-archive <cache>/td-<commit>.tar.gz
```

`facts` prints the `tdlib_source` record (the commit from the archive's pax comment, the URL, bytes, sha256,
member count and unpacked size), plus the `td_api.tl` sha256, TDLib version and commit time that the archive
implies. It refuses the archive unless every member other than a directory is a regular file under
`td-<commit>/` and all members share one mtime. The candidate commit is the commit the archive names, not a
branch name.

In one reviewed commit on the upgrade branch:

- Replace `tdlib_source` in `packaging/payload-lock.json` with the `facts` record (all keys except
  `schema_sha256`, `tdlib_version` and `source_date_epoch`).
- Add the candidate's row at index 0 of `APPROVED_PINS` and keep the current row after it, so the rollback
  path stays open. Fill in commit, version, schema sha256, minimum OS, ABI, license URL, OpenSSL version and
  build method. The `unsigned` triple stays an empty tuple until step 5, which also fills in `team_ids`.
  Move `COMMIT`, `SCHEMA_SHA256` and the v1 grammar literals to the candidate.
- Update `config.py`'s `EXPECTED_TDLIB_*` values and `prepare_native.py`'s keg constants. Update the
  `entitlement_matrix(version=…)` default in `packaging/sign_payload.py`, and every wording site that
  `git grep` finds.

### 2. Build it reproducibly

There are two routes, both from pinned inputs.

**Shipped natives (`prepare_native.py`).** The owner installs the candidate's Homebrew keg on the build Mac
(`HEAD-<first 7 hex>`, its receipt naming `<commit>`). Set `HOMEBREW_NO_INSTALL_CLEANUP=1`, so that the old
TDLib keg and the pinned OpenSSL keg stay installed for the rollback path. Then, with the extracted
candidate source as `<source tree>`:

```bash
PYTHONPATH=src .venv/bin/python packaging/prepare_native.py --output-directory <evidence>/native-1 --tdlib-source <source tree>
cp <evidence>/native-1/tdlib-lock.json packaging/tdlib-lock.json
PYTHONPATH=src .venv/bin/python packaging/prepare_native.py --output-directory <evidence>/native-2 --tdlib-source <source tree>
```

The tool refuses with a fixed code (`keg_mismatch`, `schema_mismatch`, `dependency_mismatch`,
`search_path_command`, `native_validation_failed`) unless the kegs, receipts, schema, dependencies and the
`getOption` version and commit probe all match row 0. The copied lock is the new `packaging/tdlib-lock.json`.
The second run must print `"reproduces_committed_lock": true` and `"differing_fields": []`.

**Source rebuild (`build_tdlib_source.py`, reproducibility evidence).** Build the candidate twice from clean
build trees at two different paths, then compare the outputs:

```bash
PYTHONPATH=src .venv/bin/python packaging/build_tdlib_source.py build \
    --source-archive <cache>/td-<commit>.tar.gz --build-root <cache>/rebuild-a \
    --output-directory <evidence>/run-a --log <evidence>/run-a.log
PYTHONPATH=src .venv/bin/python packaging/build_tdlib_source.py build \
    --source-archive <cache>/td-<commit>.tar.gz --build-root <cache>/another/rebuild-b \
    --output-directory <evidence>/run-b --log <evidence>/run-b.log
PYTHONPATH=src .venv/bin/python packaging/build_tdlib_source.py compare --first <evidence>/run-a --second <evidence>/run-b
```

`build` checks the archive against `payload-lock.json` and the schema against the row. It configures fixed
cmake flags (Release, the formula's `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`, the pinned OpenSSL keg, the row's
minimum macOS, no rpath, optional packages disabled) and builds only the `tdjson` target with these settings:

- `SOURCE_DATE_EPOCH` set to the archive's commit time;
- `ZERO_AR_DATE`;
- a closed environment with a private `HOME` and `TMPDIR`;
- `-ffile-prefix-map` for every build path.

Then it applies `prepare_native.py`'s relocation, dependency checks and ad-hoc signature, and runs the
`getOption` probe. An archive has no `.git`, so the tool writes a detached `.git/HEAD` naming the commit, the
value TDLib's CMake reads for its `commit_hash` option.

The output holds `native/`, the generated `include/td/telegram/td_api.h`, `notices/`, `provenance/` and
`source-rebuild-lock.json`. That last file is a document with the v1 lock's keys whose build method is
`source-rebuild`. The v1 grammar refuses it, so it never replaces `packaging/tdlib-lock.json`. The build tree
is removed after a successful build and kept after a failed one, and all tool output goes only to `--log`.
`compare` exits 0 only when every name, mode and byte is equal.

The source-built `libtdjson` is not expected to equal the Homebrew one. Homebrew's implicit build flags are
not captured (the lock says `homebrew-std_cmake_args-not-captured`), and the toolchain recorded in the lock
can differ from the build Mac's current one. The comparison with the committed lock is reported
(`equals_committed_artifact`) as information, not as a criterion.

### 3. Diff `td_api.tl` against every allowlisted request

```bash
PYTHONPATH=src .venv/bin/python packaging/build_tdlib_source.py requests \
    --old <current source tree>/td/generate/scheme/td_api.tl --new <source tree>/td/generate/scheme/td_api.tl
```

For each request in `_ALLOWED_REQUEST_TYPES`, the report says whether its definition is `unchanged`,
`changed`, `removed`, `added` or `absent` (defined in neither schema). It gives names only and exits 3 when
anything changed or was removed. For each changed or removed request, read the two definitions with
`git diff --no-index` of the two files. Then update the request builder and its shape check in
`src/telegram_search_mcp/tdjson.py` or `auth_transport.py`. The allowlist changes only through a tested
diff: `_ALLOWED_REQUEST_TYPES` and the narrower sets change in the same commit as the frozen sets in
`tests/test_caption_edit.py` and `tests/test_reply_families_058.py`, together with a test of each new or
changed request shape. An upgrade never adds a request for convenience.

At the current pin, 34 of the 35 allowlisted requests are defined. `checkDatabaseEncryptionKey` is reported
`absent`: it answers `authorizationStateWaitEncryptionKey`, a state that the pinned schema no longer has, so
the request is never sent. Removing it is a separate reviewed allowlist diff.

The request diff does not cover constructors. Step 4 and the full suite cover the fields that the client
sends and reads.

### 4. Re-run the media schema pins

The schema pins compare every content object, archive move, text edit and chat-list read that the client
builds with the constructors in the generated `td_api.h`. Without relinking Homebrew, point them at the
candidate's header from step 2:

```bash
TGMCP_TDLIB_HEADER=<evidence>/run-a/include/td/telegram/td_api.h HOME="$TMPDIR/tgmcp-home" \
    .venv/bin/python -m pytest -p no:cacheprovider -W ignore::DeprecationWarning \
    tests/test_tdlib_media_schema.py tests/test_album_send.py
```

Without the variable, the pins read the linked keg's header (`/opt/homebrew/opt/tdlib/include/td/telegram/td_api.h`)
and skip when it is missing. A variable that names no file fails the run. The release suite must show
0 skipped, so the linked keg must be the candidate's when step 6 runs. Re-check each note that `git grep`
listed in step 1 against the candidate's sources.

### 5. Re-sign and repin under the new `APPROVED_PINS` row

Fill in the candidate row's `unsigned` triple from the new `packaging/tdlib-lock.json` (names, bytes and
sha256 in lock order). Fill in `team_ids` with the Developer ID teams approved to re-sign it; that is the
owner's decision. Commit the v1 lock and the row. `repin_native.py` reads `HEAD`'s lock, never the working
tree's:

```bash
PYTHONPATH=src .venv/bin/python packaging/repin_native.py --output <evidence>/tdlib-lock.v2.json \
    --unsigned-directory <evidence>/native-1/native --signed-directory <signed>/native [--team-id <team>]
```

It refuses (`unsigned_mismatch`, `signed_refused`, `signature_invalid`, `pin_not_approved`) unless every
unsigned input equals its v1 pin, every signed output verifies, and exactly one `APPROVED_PINS` row admits
the resulting v2 lock. The release build runs the same steps itself: `packaging/release_build.py build
--dry-run` for the ad hoc kit, and the owner's Developer ID build (see
[releasing.md](releasing.md#4-release-assets-signing-and-the-release-commit-053-and-later) and the
[owner checklist](plans/0.53-signing-owner-checklist.md)).

### 6. Full suite and the consumer matrix

With the candidate keg linked:

```bash
mkdir -p "$TMPDIR/tgmcp-home" && chmod 700 "$TMPDIR/tgmcp-home"
HOME="$TMPDIR/tgmcp-home" .venv/bin/python -m pytest -p no:cacheprovider -W ignore::DeprecationWarning
.venv/bin/python packaging/release_consumer_matrix.py run --commit <sha> --receipt <absolute path>
.venv/bin/python packaging/release_consumer_matrix.py check --receipt <path>
```

The suite must show 0 failed and 0 skipped. Then run the release gate with the bundle stage on the
candidate's commit (`release_gate.py run … --bundle-native-directory <evidence>/native-1 --bundle-cache
<cache>`, then `verify`; see [releasing.md](releasing.md#3-release-gate)).

### 7. A real read-only check on the owner's account

The owner runs this check on the approved account, with the candidate installed from the dry-run kit or a
source installation that loads the candidate keg. Before the first start, save the account's session
directory as the [security rollback procedure](security.md#rollback) describes. A newer TDLib may migrate
the session database, and an older TDLib is not guaranteed to open the migrated copy.

Through the official SDK or a real MCP client, check four things:

- the broker starts;
- the `getOption` probe answers the candidate version and commit;
- `list_chats`, `search_messages` for a known phrase and `read_messages` of a known message return what the
  current pin returns;
- an attachment of a known message still reads.

Do not send, edit, archive, pin or delete anything. Record the date, the consumer and the result outside
the public tree, and record only that the check passed in the public notes.

### 8. Rollback path to the old pin

- **Before the merge:** abandon the branch. Nothing that ships has changed.
- **After the merge, before a release:** revert the upgrade commits. The old row, the old source archive
  record and the old kegs were kept, so `prepare_native.py` reproduces the old lock byte for byte
  (`"reproduces_committed_lock": true`), and `build_tdlib_source.py` rebuilds the old commit from its
  archive.
- **After an installed upgrade:** the installed launcher's `rollback` returns to the previous release with
  its own natives and lock while no newer session state exists ([install guide](install.md#rollback)).
  Otherwise, restore the session directory saved at step 7 together with the previous runtime, as the
  [security rollback procedure](security.md#rollback) describes. A source installation also relinks the
  kept old keg on the build Mac, which is an owner step.
- The old `APPROVED_PINS` row stays until no supported release ships that pin. It is removed only in a
  later reviewed commit.
