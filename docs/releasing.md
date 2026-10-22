# Releasing

[Overview](../README.md) · [Roadmap](roadmap.md) · [Security and rollback](security.md#rollback) · [Release preparation tools (frozen 0.51.0)](release-preflight.md)

The checklist for cutting a release of this repository, from the pre-cut conditions to the
post-release version bump. Every step names a command or a file that exists at the release
commit. Run it from the repository root of a clean checkout with the development environment
from `CLAUDE.md` (`.venv/bin/python`). Synthetic and automated evidence is technical evidence
only: real Telegram, installed-consumer and owner-session rows are recorded separately and
disclosed as such in the release notes.

What is released: the source tree, its sdist and wheel, and the documents in `docs/`. The
private bundle that carries the application identity is never part of the public tree or of the
release artifacts; its build (`packaging/assemble_bundle.py`) is owner-run and outside this list.

## 1. Version sites

The release version appears in these places. A version-consistency test
(`tests/test_version_consistency.py`) keeps them equal; this list is the human-readable mirror,
and the test is the authority if the two differ.

| Site | What it holds |
| --- | --- |
| `src/telegram_search_mcp/__init__.py` | `__version__`, the single source for the package version (`pyproject.toml` reads it) |
| `packaging/launcher.py` | the bundle admission literal (`release_version`) and the help text |
| `tests/test_bundle_archive.py`, `tests/test_bundle_install.py` | the synthetic release fixtures |
| `skills/telegram-search/SKILL.md`, `skills/telegram-setup/SKILL.md` | the "paired with" version line of each skill |
| `README.md`, `CHANGELOG.md` | the status paragraph and the top changelog heading |
| `docs/install.md`, `docs/reference.md`, `docs/roadmap.md`, `docs/security.md`, `docs/licensing.md`, `docs/third-party-notices.md` | candidate or release wording and the `expected_package_version` examples |
| `docs/release-notes-<version>.md` | the notes of this release; new for every release |

A new `docs/release-notes-<version>.md` and `packaging/constraints-<version>.txt` must also be
listed in `pyproject.toml` (`[tool.setuptools.data-files]`) and `MANIFEST.in`, as this document and
`packaging/release_gate.py` are; the gate refuses an sdist that misses a tracked public file. To
find stragglers: `git grep -n -F '<previous dev version>'`.

`expected_package_version` in a user's `runtime.toml` is a per-installation pin, not a version
site; see section 8. The tests of the frozen 0.51.0 harness (`tests/test_release_*.py`) keep their
0.51.0 fixtures on purpose and are not version sites.

## 2. Pre-cut conditions

All must hold before the gate runs; the release commit is the commit that holds them.

1. **Clean tree.** `git status --porcelain` prints nothing and `git rev-parse HEAD` is the commit
   to release. Untracked local notes (`CLAUDE.local.md`) are gitignored and not part of it.
2. **Critic review closed.** An independent review of `git diff <previous-tag>..HEAD` has no open
   finding: each is fixed, or recorded under "Known limits" in the release notes.
3. **Full suite with the build installed, isolated HOME.** The development venv must include the
   pinned build frontend and backend
   (`.venv/bin/pip install -e . pytest tomlkit==0.13.3 build==1.3.0 setuptools==80.9.0`): the
   release package tests build with `--no-isolation` and skip when either is missing. The suite
   reads the live policy file, so it runs with an isolated home, and its summary must show 0 failed
   and 0 skipped:

   ```bash
   mkdir -p "$TMPDIR/tgmcp-home" && chmod 700 "$TMPDIR/tgmcp-home"
   HOME="$TMPDIR/tgmcp-home" .venv/bin/python -m pytest -p no:cacheprovider -W ignore::DeprecationWarning
   ```

   Never run `pip install .` in a checkout; it leaves `build/` and changes which tests run.
4. **Privacy guard green.** `tests/test_public_tree_privacy.py` passes (section 4).
5. **Versions agree.** `tests/test_version_consistency.py` passes with the release version.
6. **Documents ready.** `CHANGELOG.md`, `docs/release-notes-<version>.md` and `docs/roadmap.md`
   follow section 5, and every unmet gate bullet is disclosed in the notes.
7. **Pins reviewed.** If a dependency pin or `packaging/tdlib-lock.json` changed since the
   previous tag, `docs/third-party-notices.md`, `docs/licensing.md` and `docs/licenses/` match, and
   `packaging/constraints-<version>.txt` pins the tested closure with hashes.

Author and committer identity and per-commit history findings are not pre-cut conditions: the
gate reports them (section 4) and publication waits for the owner's history decision when the
identity is not the public one or `history_findings` is not 0.

## 3. Release gate

```bash
.venv/bin/python packaging/release_gate.py run --commit <sha> --previous-tag <tag> --workdir <dir>
.venv/bin/python packaging/release_gate.py verify --evidence <dir>/evidence.json
```

Both take `--repo <checkout>` (default: the repository of the current directory). A refusal prints
one fixed code (`release gate refused: <code>`) and never echoes an argument; `run` keeps the
details in `<dir>/refusal.json`. Both must exit 0.

- `<tag>` is the previous release tag, `v<major>.<minor>.<patch>` or a candidate
  `v<major>.<minor>.<patch>rc<n>`, and must be an ancestor of `<sha>`.
- `<dir>` is an absolute path that is missing or empty, outside the repository and outside the
  temp roots the OS clears: `/tmp`, `/var/folders` (the macOS `TMPDIR`) and their `/private`
  forms. Paths are compared case-insensitively and by file identity (device and inode) of every
  existing ancestor, so `/PRIVATE/TMP/x` and firmlink aliases such as
  `/System/Volumes/Data/private/tmp/x` or `/System/Volumes/Data<repository>/x` are refused too.
- `run` builds everything from `git archive <sha>`, never from the working tree. Its only network
  use is pip downloading the hash-pinned build and test tools into `<dir>/toolhouse` and the
  closure of `packaging/constraints-<version>.txt` into `<dir>/wheelhouse`. In order, it downloads
  the tools, builds the sdist and the wheel twice and requires identical bytes, checks their
  members against the tracked files, runs the privacy scan of section 4 (a hit refuses before the
  closure is downloaded or anything is installed), downloads the closure, installs it offline with
  `pip check` and every `RECORD` hash, smoke-tests the installed server over stdio, then runs the
  full suite from a fresh archive extraction in a fresh offline venv with a hermetic HOME. It
  writes `<dir>/evidence.json`, `<dir>/dist/`, `<dir>/junit.xml` and `<dir>/suite.log`.
- `verify` binds to the **recorded** commit, not to `HEAD`, so old evidence stays verifiable. It
  accepts only the exact bytes `run` wrote, checks the artifacts and the junit and log files on
  disk, rebuilds from a fresh `git archive` offline from `<dir>/toolhouse` and `<dir>/wheelhouse`,
  re-derives every field and re-runs the full suite in a fresh offline venv with a hermetic HOME;
  passed, failed and skipped counts that differ from the evidence refuse with
  `evidence_mismatch (tests)`. Evidence written by another gate version is verified by the gate
  stored at the recorded commit. Run it again after copying the evidence or the artifacts anywhere.
- Any later commit, even a documentation fix, is a new release commit and needs a new `run`.
- `release_preflight` and `release_sdk_probe` are the frozen 0.51.0 harness and do not certify
  0.52.x; see [release-preflight.md](release-preflight.md).

The evidence is a local artifact: keep it with the release record outside the public tree (its
`identities` hold names and addresses, its `history_findings` paths), and quote digests and
results, never paths, in the notes.
Its keys:

| Key | What it records |
| --- | --- |
| `schema` | `tgmcp-release-gate/v1` |
| `gate` | `sha256` of the gate that ran and `at_commit_sha256` of `packaging/release_gate.py` at the commit |
| `commit`, `tree`, `commit_time` | the full commit id, its tree id and its commit time (the build epoch) |
| `previous_tag`, `previous_commit` | the tag given to `run` and the commit it names |
| `version` | `__version__` at the commit |
| `toolchain` | `python`, `implementation`, `platform`, `pip` and the pinned `tools` (name, version, SHA-256) |
| `archive` | `sha256` of `git archive <sha>` and its file count |
| `sdist`, `wheel` | `file` (under `dist/`), `sha256`, `bytes` and `members` |
| `constraints` | `file`, `sha256`, `target` interpreter, `distributions` and the downloaded `wheels` with digests |
| `install` | `freeze` of the installed closure, `pip_check` and the number of verified `record_entries` |
| `smoke` | `server_version`, `tool_count`, `tools_sha256`, `schema_fingerprint` and `capabilities` seen over stdio |
| `privacy` | the `guard` file and its `guard_sha256`, its `rules`, the `allow` entry count, `scanned` counts and `hits` (empty) |
| `history_findings` | the per-commit history report of section 4: `commits`, `files` and `lines` scanned, the `total` and the first 2000 `findings` as `<12-hex commit>:<path>:<line>:<rule>`; a report, not a refusal |
| `identities` | `commits` in the range and each author, committer and co-author with role, `class` (`noreply` or `direct`) and commit count |
| `tests` | `tests`, `passed`, `failed`, `errors`, `skipped`, the `skips` with reasons, the last log line (`summary`), and `junit`, `log` with their SHA-256 |

## 4. Privacy and identity

**Public tree.** It carries no private account data, local paths or internal working records
(the standard of [the 0.51.0 notes](release-notes-0.51.0.md)). `tests/test_public_tree_privacy.py`
scans every file `git ls-files` lists (a tree without `.git` is walked) and fails on local user
and home paths, another user's tilde home (`~` and a user name), Claude project folder names,
working-directory names of private lineages and research folders, message and chat identifiers,
international phone numbers, `api_hash` values, e-mail addresses other than the accepted noreply
addresses (`noreply@users.noreply.github.com`, `noreply@anthropic.com`, `noreply@github.com`) and
reserved example domains, and the running account's own home or checkout path. The patterns are the `RULES`
table in that file; its `ALLOW` table is the whole allowlist, each entry with a reason. Every entry
must admit at least one finding of the scanned tree and a path-scoped entry must name a file that
still exists, otherwise the test fails. Account names, message ids, session clock times, archive
and index digests and operational logs stay outside the repository. Machine-specific notes go to
an untracked, gitignored `CLAUDE.local.md`.

**Gate scan.** `run` loads that same file from `git archive <sha>` and runs its `scan_text`, with
its `ALLOW` table, over every text member of the sdist and the wheel (under their tracked paths, so
path-scoped entries apply), the lines added by `git diff <tag>..<sha>` and every commit message of
`git log <tag>..<sha>`. A missing guard, or one without `scan_text`, refuses with
`privacy_guard_missing`; any finding refuses with `privacy_hit`, and `refusal.json` lists only
`path:line:rule`, never the matched text (a path that is itself a finding appears as
`name-sha256-<first 16 hex digits of its SHA-256>`).

**History scan.** The tip diff misses text that a commit of the range added and a later one
removed, yet that text stays in the published history. `run` therefore also runs the same guard
over the names and lines each commit of `git log -p <tag>..<sha>` added (a merge contributes what
its conflict resolution added, git's remerge diff). Its findings are a classified report in
`history_findings` (`<12-hex commit>:<path>:<line>:<rule>`, never the value), not a refusal:
history remediation is the owner's decision, taken before publication (Gpub, section 7).

**Identity.** New commits and tags use the repository-local public identity of the v0.51.0 tag
(name `Unofficial Telegram MCP Contributors`, address `noreply@users.noreply.github.com`),
never a global or personal one. The gate records every author, committer and co-author of the
range in `identities`, classified `noreply` or `direct`. That is a report, not a gate step and not
a pre-cut condition: if anything other than the public identity appears, the release may still be
cut and verified, but publication waits for the owner's history decision (see Gpub below); this
checklist never rewrites history on its own. The same list from git:

```bash
git log --format='%an <%ae> | %cn <%ce>' <tag>..<sha> | sort -u
```

## 5. Changelog, release notes and roadmap

- **`CHANGELOG.md`**, newest first. The development heading `## <version>.dev0 — unreleased`
  becomes `## <version> — <YYYY-MM-DD>` in the release commit. Entries describe behaviour users
  can observe, mark owner decisions, and separate synthetic from real evidence.
- **`docs/release-notes-<version>.md`**, modelled on [the 0.51.0 notes](release-notes-0.51.0.md):
  the title `# <version> — <date>`, the navigation line, then Available behavior, Corrections
  included, Accepted scenarios (what was exercised, by which consumer, with real or synthetic
  providers), Known limits (including every unmet gate bullet of the release plan), and License
  and public identifiers. Include the upgrade note of section 8. Frozen after the tag, except
  for broken links.
- **`docs/roadmap.md`**: update the "Updated" date, move the candidate section into the completed
  slices, refresh the status rows, and link this document. Technical acceptance of a slice does
  not complete the plan (see "Maintaining this record" there).

## 6. Tag

Tag only after `verify` passes, and only locally. Tags are annotated, never lightweight, and
unsigned like `v0.51.0`: the name is `v<version>` for a release (`v<version>rcN` for a candidate),
the first message line is `<version>: <short theme>`, the tagger is the public identity.

```bash
git tag -a v<version> -m "<version>: <short theme>" <sha>
git cat-file -t v<version>                       # tag
git for-each-ref refs/tags/v<version> --format='%(taggername) <%(taggeremail)> %(object)'
git rev-parse v<version>^{commit}                # equals the commit in evidence.json
```

## 7. Publication (Gpub)

Gpub is an explicit owner authorization naming the refs and artifacts to publish. Until it is
given, nothing is pushed to `main` and no tag leaves the machine; working branches may be pushed
as backups. It also needs the owner's decision on the history and identity question of section 4,
because moving `main` forward makes the cheaper remediation (rewriting only the unmerged branch
range) impossible without rewriting `main`. Gpub checks `history_findings` in the verified
evidence: its `total` must be 0, or the owner's acceptance of the listed findings (or the
remediation chosen, which makes a new release commit and a new `run`) is recorded with the release
record before anything is pushed. Pushed objects stay retrievable on the remote even
after a ref is deleted, so publication is irreversible in practice.

Remote pre-checks (read-only):

```bash
git remote -v                                         # the expected remote only
git fetch origin
git ls-remote origin refs/heads/main 'refs/tags/*'    # record this output
git merge-base --is-ancestor origin/main <sha> && echo fast-forward
git ls-remote --tags origin v<version>                # must print nothing
.venv/bin/python packaging/release_gate.py verify --evidence <dir>/evidence.json
.venv/bin/python -c 'import json, sys; print(json.load(open(sys.argv[1]))["history_findings"]["total"])' \
    <dir>/evidence.json                               # 0, or the owner's acceptance is recorded
```

Publish only the named refs, one push each, never `--force` and never `--tags`:

```bash
git push origin <sha>:refs/heads/main
git push origin refs/tags/v<version>
```

The release is source-only unless the authorization also names artifacts. If it names the sdist or
wheel, upload exactly the files `run` produced and compare their SHA-256 with the evidence. There
is no package-index upload by default.

Remote post-checks:

```bash
git ls-remote origin refs/heads/main refs/tags/v<version> 'refs/tags/v<version>^{}'
git clone --branch v<version> <remote-url> <fresh-dir-outside-tmp>
git -C <fresh-dir-outside-tmp> rev-parse HEAD 'HEAD^{tree}'   # commit and tree equal the evidence
```

`main` and the peeled tag must both equal the released commit, and the clone's tree hash must equal
the one in the evidence.

## 8. Upgrade note: `runtime.toml` pins

A broker and proxy refuse (`config_stale`) when the policy file pins another package version. The
release notes of every version therefore tell installed users: after upgrading, set
`expected_package_version` in `~/Library/Application Support/TelegramSearchMCP/runtime.toml` to the
new version, keep `expected_contract_version` and the other lines, and keep the modes (`0700`
directory, `0600` file). A policy written by `connect` pins the version that wrote it. A running
client session keeps its old proxy and is refused until its MCP server restarts; clients refresh
their tool listing after an upgrade. Never overwrite an existing policy blindly; see "Enabling more
capabilities" in [install.md](install.md) and the policy example in [reference.md](reference.md).

## 9. Rollback

Installed runtime: follow "Rollback" in [security.md](security.md#rollback). Before an upgrade,
save the previous runtime, the owner-only plist, the consumer config and the installed skill in a
private directory outside the source tree; restore them and re-bootstrap only the validated
LaunchAgent. It leaves TDLib session data, credentials and historical data in place. The security
document also records the limit that matters across the 0.52 line: once a newer version has written
session state, an older binary refuses it, so a rollback needs the saved pre-upgrade files.

Published release: a pushed tag is not retracted. Fix forward with a new patch version through this
checklist; deleting a remote tag or branch is a separate owner-authorized action and does not remove
the objects.

## 10. After the release

Open the next development line in one commit by the public identity, on top of the tagged commit
(the tag is never moved):

1. Set `__version__` to the next development version (`<next>.dev0`) and update the version
   sites of section 1 that carry a development literal.
2. Add `## <next>.dev0 — unreleased` on top of `CHANGELOG.md`.
3. Point the README status and `docs/roadmap.md` at the new candidate.
4. Run the full suite as in section 2; the version-consistency test must pass.

Release evidence is never edited: a corrected release is a new commit, a new `run` and a new tag.

### Consumer matrix (from the 0.54 candidate)

```bash
.venv/bin/python packaging/release_consumer_matrix.py run --commit <sha> --receipt <absolute path>
.venv/bin/python packaging/release_consumer_matrix.py check --receipt <path>
```

`run` installs the wheel built from `git archive <sha>` with the pinned closure in a fresh venv and drives
every synthetic cell of the [consumer matrix](plans/0.54-consumer-matrix-spec.md) through the official SDK
against synthetic brokers; it writes the receipt only when every cell passed. `check` rebuilds the wheel
and the schema census of the recorded commit and refuses any difference. The receipt is `acceptance:
false`: owner and real-account cells stay `pending` and need their own records.

## Checklist

| Step | Command or file |
| --- | --- |
| Version sites equal | `tests/test_version_consistency.py` |
| Clean tree, release commit | `git status --porcelain`, `git rev-parse HEAD` |
| Full suite, isolated HOME, `build==1.3.0` and `setuptools==80.9.0` installed, 0 skipped | command in section 2 |
| Privacy guard | `tests/test_public_tree_privacy.py` |
| Changelog, notes, roadmap | `CHANGELOG.md`, `docs/release-notes-<version>.md`, `docs/roadmap.md` |
| Package data | `pyproject.toml`, `MANIFEST.in` |
| Gate | `.venv/bin/python packaging/release_gate.py run`, then `verify` |
| Consumer matrix (synthetic cells) | `.venv/bin/python packaging/release_consumer_matrix.py run`, then `check` |
| Identity report (not a pre-cut condition) | `identities` in `evidence.json`; owner decision before Gpub |
| History report (not a pre-cut condition) | `history_findings.total` in `evidence.json` is 0 or the owner's acceptance is recorded before Gpub |
| Tag | `git tag -a v<version> -m ...` |
| Gpub with remote pre and post checks | section 7 |
| Pin note for users | section 8, in the release notes |
| Rollback | `docs/security.md#rollback` |
| Dev bump | section 10 |
