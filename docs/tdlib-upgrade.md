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
| `src/telegram_search_mcp/native_runtime.py` | `COMMIT`, `SCHEMA_SHA256` and `APPROVED_PINS`. Each pin is one reviewed row: commit, version, schema sha256, minimum OS, ABI, license URL, OpenSSL version, build method, the unsigned artifacts (name, bytes, sha256) and the approved Developer ID teams. The v1 lock grammar (`NativeManifest`, `BuildProvenance` with its `openssl_version`, `NativeNotices` with its license URL) admits exactly one pin through its literals. Row 0 is the pin `prepare_native.py` and `build_tdlib_source.py` build. |
| `packaging/tdlib-lock.json` | The v1 lock of the unsigned natives. Only `prepare_native.py` writes it; it is never edited by hand. |
| `packaging/payload-lock.json` (`tdlib_source`) | The source archive: commit, URL, bytes, sha256, member count and unpacked size. The bundle ships it as corresponding source, and `build_tdlib_source.py` builds from it. |
| `packaging/payload-lock.json` (`native_notices`) | The OpenSSL notices the bundle ships: `openssl_version` and the bytes and sha256 of `LICENSE.txt`, `AUTHORS.md` and `README.md`, which `prepare_native.py` copies from the keg. `packaging/prepare_payload.py` (`native_tree`, the release gate's bundle stage) refuses `native_mismatch` unless `openssl_version` equals the lock's `build.openssl_version` and every notice equals its hash. `packaging/release_build.py` writes `references/openssl.json` from `openssl_version`. |
| `packaging/prepare_native.py` | `TDLIB_KEG`, `OPENSSL_KEG` and `OPENSSL_OPT`: the kegs the shipped natives come from. |
| `src/telegram_search_mcp/config.py` | `EXPECTED_TDLIB_KEG`, `EXPECTED_TDLIB_VERSION` and `EXPECTED_TDLIB_COMMIT`, which a Homebrew or source installation checks with the `getOption` probe. |
| `src/telegram_search_mcp/tdjson.py` | `_ALLOWED_REQUEST_TYPES` and the narrower sets (`_CHANGE_REQUEST_TYPES`, `_DELETE_REQUEST_TYPES`, `_CALL_REFUSED_TYPES`, `_AUTHORIZATION_CONTROL_TYPES`). They are frozen by `tests/test_caption_edit.py` (`ALLOWED`) and `tests/test_reply_families_058.py` (`ALLOWED_0_57`). |
| `tests/test_tdlib_media_schema.py`, `tests/test_album_send.py` | The schema pins: the exact request objects the client builds, compared with the generated `td_api.h` through `tests/tdlib_header.py`. |
| Notes tied to the pin | Behaviour that was verified against the pinned sources, such as `src/telegram_search_mcp/auth_transport.py`, `outgoing_drafts.py`, `outgoing_formatting.py`, `send_observations.py` and `bundle_runtime.py`, plus the wording in `docs/install.md`, `docs/reference.md`, `docs/security.md`, `docs/licensing.md` and `docs/third-party-notices.md` (the last two name the OpenSSL version). |

`git grep -n -F -e d1085f9 -e 1.8.67 -e 3.6.4`, with the current pin's commit, TDLib version and OpenSSL
version, lists every file above that names the pin, `native_notices` included. It also lists the test
fixtures that name the pin, and the plan records under `docs/plans/`, which stay as written. `tdjson.py`
and the schema-pin tests name no pin value; steps 3 and 4 cover them.

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
- Keep the current row of `APPROVED_PINS` with its own values, so the rollback path stays open. It is built
  from `COMMIT` and `SCHEMA_SHA256` (its commit, schema sha256 and license URL), which the next bullets
  move. So first rewrite those three fields as literals with the values they have now: today
  `'d1085f9cebc5a62379991ae1652673954f229c1f'`, `'326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87'`
  and `'https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/LICENSE_1_0.txt'`.
- Add the candidate's row at index 0, before the kept row. Fill in commit, version, schema sha256, minimum
  OS, ABI, license URL, OpenSSL version and build method. The `unsigned` triple stays an empty tuple until
  step 5, which also fills in `team_ids`. Then move `COMMIT`, `SCHEMA_SHA256` and the v1 grammar literals
  (`NativeManifest`, `BuildProvenance.openssl_version`, `NativeNotices.tdlib_license_source`) to the
  candidate. Check that the rows stay distinct and that row 0 is the candidate:

  ```bash
  PYTHONPATH=src .venv/bin/python -c "from telegram_search_mcp.native_runtime import APPROVED_PINS as P; assert len({r.key() for r in P}) == len(P); print([(r.source_commit[:7], r.tdlib_version, r.openssl_version) for r in P])"
  ```
- Update `config.py`'s `EXPECTED_TDLIB_*` values and `prepare_native.py`'s keg constants. Update the
  `entitlement_matrix(version=…)` default in `packaging/sign_payload.py`, and every site that the search
  under "Where the pin lives" finds. `native_notices` follows in step 2.

**An OpenSSL-only advisory** keeps `tdlib_source`, `COMMIT`, `SCHEMA_SHA256`, `TDLIB_KEG` and `config.py`.
The candidate row repeats the current row with the new `openssl_version`, so both rows name one commit;
both build tools build row 0. It moves `BuildProvenance.openssl_version` and `OPENSSL_KEG`, and step 2
refills `native_notices`. The search for the old OpenSSL version (`git grep -n -F -e 3.6.4` today) lists
the sites, including `docs/licensing.md` and `docs/third-party-notices.md`.

### 2. Build it reproducibly

**The source tree.** `prepare_native.py --tdlib-source` and step 3 read an extracted source tree. Extract
it from an archive that `facts` accepted (regular files under `td-<commit>/` only):

```bash
mkdir -m 700 <cache>/src-<commit>
tar -xzf <cache>/td-<commit>.tar.gz -C <cache>/src-<commit>
```

`<source tree>` is then `<cache>/src-<commit>/td-<commit>`. The current pin's tree, for step 3's `--old`,
comes the same way from its own archive: the `url` of `tdlib_source` on the main branch, fetched into
`<cache>` and checked against that record's sha256 with `shasum -a 256` before `tar`. A tree kept by
`build --keep-build-tree` is not a source tree: TDLib writes generated sources into it, and the tool adds
`.git/HEAD`.

There are two routes, both from pinned inputs.

**Shipped natives (`prepare_native.py`).** The owner installs the candidate's Homebrew keg on the build Mac
(`HEAD-<first 7 hex>`, its receipt naming `<commit>`). Set `HOMEBREW_NO_INSTALL_CLEANUP=1`, so that the old
TDLib keg and the pinned OpenSSL keg stay installed for the rollback path. Then:

```bash
PYTHONPATH=src .venv/bin/python packaging/prepare_native.py --output-directory <evidence>/native-1 --tdlib-source <source tree>
cp <evidence>/native-1/tdlib-lock.json packaging/tdlib-lock.json
PYTHONPATH=src .venv/bin/python packaging/prepare_native.py --output-directory <evidence>/native-2 --tdlib-source <source tree>
```

The tool refuses with a fixed code (`keg_mismatch`, `schema_mismatch`, `dependency_mismatch`,
`search_path_command`, `native_validation_failed`) unless the kegs, receipts, schema, dependencies and the
`getOption` version and commit probe all match row 0. The copied lock is the new `packaging/tdlib-lock.json`.
The second run must print `"reproduces_committed_lock": true` and `"differing_fields": []`.

When the OpenSSL version changes, set `native_notices` in `packaging/payload-lock.json` from the first run:
`openssl_version`, and the bytes and sha256 of the three notices.

```bash
wc -c <evidence>/native-1/notices/openssl-<version>/LICENSE.txt <evidence>/native-1/notices/openssl-<version>/AUTHORS.md <evidence>/native-1/notices/openssl-<version>/README.md
shasum -a 256 <evidence>/native-1/notices/openssl-<version>/LICENSE.txt <evidence>/native-1/notices/openssl-<version>/AUTHORS.md <evidence>/native-1/notices/openssl-<version>/README.md
```

An upgrade that keeps OpenSSL keeps `native_notices`: the notices are the keg's files. Either way the release
gate's bundle stage refuses `native_mismatch` (step 6) while `native_notices` disagrees with the lock or the
notices.

For an OpenSSL-only advisory, the owner installs the new `openssl@3` keg and a TDLib keg of the same commit
whose receipt names the new OpenSSL as its dependency (`keg_mismatch` otherwise). That TDLib keg replaces the
old one, since both are named `HEAD-<first 7 hex>`. So before it is installed, keep the current pin's output,
made from a checkout of the main branch, where row 0 is still the current pin
(`prepare_native.py --output-directory <evidence>/native-old --tdlib-source <current source tree>`, which
reproduces that branch's committed lock). It is the old lock's native directory for step 8.

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

`build` checks the archive's length and sha256 against `payload-lock.json` before it creates the log, the
build root or the output, then its members and the schema against row 0. It configures fixed
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
Each cmake step runs in its own process group, which is killed with the step on a timeout (refused
`build_failed`) or on Ctrl-C, SIGTERM or SIGHUP (refused `build_interrupted`; a signal the tool inherited as
ignored, as under `nohup`, stays ignored), so no compiler outlives the tool. `compare` exits 0 only when every
name, mode and byte is equal.

The source-built `libtdjson` is not expected to equal the Homebrew one. Homebrew's implicit build flags are
not captured (the lock says `homebrew-std_cmake_args-not-captured`), and the toolchain recorded in the lock
can differ from the build Mac's current one. The comparison with the committed lock is reported
(`committed_lock`, `equals_committed_artifact`, `differing_fields`) as information, not as a criterion. A
committed lock that the v1 grammar no longer reads (after step 1, before the `cp` above) is reported as
`"committed_lock": "unreadable"`, never refused.

### 3. Diff `td_api.tl` against every allowlisted request

Both trees are extracted as step 2 describes ("The source tree"):

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
and skip when it is missing. A variable that is set but names no regular file fails the run, an empty value
included, so `TGMCP_TDLIB_HEADER=$NAME` with `NAME` unset never tests the keg's header by mistake. The
release suite must show 0 skipped, so the linked keg must be the candidate's when step 6 runs. Re-check
each note that the search listed in step 1 against the candidate's sources.

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
<cache>`, then `verify`; see [releasing.md](releasing.md#3-release-gate)). The bundle stage refuses
`native_mismatch` unless the natives equal the committed lock, and `native_notices` equals the lock's
OpenSSL version and the notices in `<evidence>/native-1`.

### 7. A real read-only check on the owner's account

The owner runs this check on the approved account, with the candidate installed from the dry-run kit or a
source installation that loads the candidate keg. A newer TDLib may migrate the session database
(`tdlib/db`) when it first opens it, and an older TDLib is not guaranteed to open the migrated copy. The
launcher's `rollback` does not undo that (step 8), so the owner saves every account's TDLib session before
the candidate's first start. The same procedure is in
[TDLib session backup and restore](security.md#tdlib-session-backup-and-restore).

**Save the session.** Quit the MCP clients, let any approval dialog close and leave no `connect` screen open.
Stopping the broker by hand skips the installer's pending-send gate. Then, for the default account and
again for each alias:

```bash
S="$HOME/Library/Application Support/TelegramSearchMCP"
LABEL="com.$(basename "$HOME").telegram-search-mcp.broker"
STAMP=<yyyymmdd of the upgrade>
launchctl bootout "gui/$(id -u)/$LABEL"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "still loaded: do not copy"
mkdir -m 700 "$S/tdlib-backup-$STAMP"
cp -pR "$S/tdlib/" "$S/tdlib-backup-$STAMP/"
diff -rq "$S/tdlib" "$S/tdlib-backup-$STAMP" && echo "copy complete"
plutil -extract key_account raw -o - "$S/enrollment.json"
```

For an alias, `S` is `$HOME/Library/Application Support/TelegramSearchMCP-accounts/<alias>` and `LABEL` is
`com.$(basename "$HOME").telegram-search-mcp.a.<alias>.broker`. `bootout` is the documented way to stop a
broker ([install guide](install.md#a-refusal-after-the-broker-stop)). The copy only reads the original, sits
next to it on the same volume, and is owner-only: `files` holds downloaded attachments unencrypted. It needs
as much free space as `du -sh "$S/tdlib"` shows.

Nothing of the Keychain is copied. The database in `tdlib/db`, and so the copy, is encrypted with the random
key under the Keychain account that `enrollment.json` names (`key_account`, `database_key_<enrollment-id>`,
service `com.<home-directory-name>.telegram-search-mcp`). `enrollment.json` and the `.managed-enrollment`
marker bind that key to this session directory, by path and by the inode (on one volume; macOS changes the device number at mount) of `tdlib/` and of the
session root. Neither an upgrade nor a rollback writes the key, `enrollment.json` or the marker, so the copy
opens with the key that stays in the Keychain. Record the `key_account` value and the backup's path with the
private record of this check. Never delete or regenerate that Keychain item while the copy may be needed.

Then install the candidate. The installer restarts only a broker that was running, so start each broker
again, then confirm it with the candidate release's `connect` (its activation command), which answers
`already_connected` (`broker_starting`: wait and run it again):

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"
```

**The check.** Through the official SDK or a real MCP client, check four things:

- the broker starts;
- the `getOption` probe answers the candidate version and commit;
- `list_chats`, `search_messages` for a known phrase and `read_messages` of a known message return what the
  current pin returns;
- an attachment of a known message still reads.

Do not send, edit, archive, pin or delete anything. Record the date, the consumer and the result outside
the public tree, and record only that the check passed in the public notes. Keep the backup until the
candidate is accepted and no rollback can be needed; deleting it is a separate owner decision.

### 8. Rollback path to the old pin

- **Before the merge:** abandon the branch. Nothing that ships has changed.
- **After the merge, before a release:** revert the upgrade commits, then relink the kept old kegs on the
  build Mac (an owner step). `config.py`'s `EXPECTED_TDLIB_KEG` check and the schema pins read the linked
  keg, so they fail or skip until `readlink /opt/homebrew/opt/tdlib` names `../Cellar/tdlib/HEAD-<old 7 hex>`
  again. The old row, the old source archive record and the old kegs were kept, so `prepare_native.py`
  reproduces the old lock byte for byte (`"reproduces_committed_lock": true`), and `build_tdlib_source.py`
  rebuilds the old commit from its archive. After an OpenSSL-only upgrade the old TDLib keg was replaced:
  the release gate takes `<evidence>/native-old` (step 2) as `--bundle-native-directory`, since it equals
  the old lock.
- **After an installed upgrade:** the installed launcher's `rollback` returns to the previous release with
  its own natives and lock ([install guide](install.md#rollback)), and a source installation goes back by
  the [manual procedure](security.md#rollback) and relinks the kept old kegs. **Neither undoes a database
  migration.** The launcher's rollback guard compares only `enrollment.json` and `runtime.toml`, which TDLib
  never writes, so it does not notice a migrated `tdlib/db`. Once the candidate has started, restore the
  session saved at step 7, with `S`, `LABEL` and `STAMP` as there:

  1. Quit the MCP clients and stop every broker, the default account's and each alias's, before the
     rollback: a rollback restarts each broker that was running, and the old TDLib would open the migrated
     database. `launchctl bootout "gui/$(id -u)/$LABEL"` for each `LABEL`.
  2. Roll the release back once: the current release's `launcher.py rollback`, which covers every account,
     or the manual procedure up to, not including, its bootstrap.
  3. For each account, move the migrated session aside (never delete it) and restore the copy. The
     enrollment binds `tdlib/` by its inode, so its contents are replaced, never the directory itself:

     ```bash
     ASIDE="$S/tdlib-migrated-$(date +%Y%m%d-%H%M%S)"
     mkdir -m 700 "$ASIDE" &&
       mv "$S/tdlib/db" "$S/tdlib/files" "$ASIDE/" &&
       test ! -e "$S/tdlib/db" && test ! -e "$S/tdlib/files" &&
       cp -pR "$S/tdlib-backup-$STAMP/db" "$S/tdlib-backup-$STAMP/files" "$S/tdlib/" &&
       diff -rq "$S/tdlib" "$S/tdlib-backup-$STAMP" && echo "restored"
     ```
  4. Start each broker (`launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"`),
     then run the restored release's `connect` for that account: it answers `already_connected` with no new
     login. If the session is no longer authorized, `connect` runs the QR flow on the existing enrollment.
     Reconnect the clients, call `_manifest` with `check_broker=true` and make one bounded read.

  Restoring by copy keeps the backup for a second attempt. Each attempt moves the current session aside under a
  new name first and copies only once `tdlib/db` and `tdlib/files` are gone, so a repeated attempt never merges
  into a session the old TDLib already wrote, and nothing is overwritten or deleted. TDLib fetches from the
  server what it had cached after the copy was made.
- **No installer guard (decision, 25.10.2026).** A cheap installer-side guard is not added now. Hashing
  `tdlib/db` cannot be one, because TDLib writes to its database whenever it runs, so every rollback would
  refuse. Reading the database version needs the Keychain key, which the installer never reads. A guard on
  the two releases' TDLib pins (refuse a rollback across a pin change until the owner has restored the
  session) could never fire today, since every release so far ships d1085f9. It must also ship in the
  launcher of the first release that changes the pin, because the newer release's launcher performs the
  rollback. So the spec of that upgrade decides it, and this procedure is the guard until then.
- The old `APPROVED_PINS` row stays until no supported release ships that pin. It is removed only in a
  later reviewed commit.
