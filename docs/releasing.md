# Releasing

[Overview](../README.md) · [Roadmap](roadmap.md) · [Security and rollback](security.md#rollback) · [Release preparation tools (frozen 0.51.0)](release-preflight.md)

The checklist for cutting a release of this repository, from the pre-cut conditions to the
post-release version bump. Every step names a command or a file that exists at the release
commit. Run it from the repository root of a clean checkout with the development environment
from `CLAUDE.md` (`.venv/bin/python`). Synthetic and automated evidence is technical evidence
only: real Telegram, installed-consumer and owner-session rows are recorded separately and
disclosed as such in the release notes.

What is released: the source tree, its sdist and wheel, and the documents in `docs/`. From 0.53 a
release can also carry the assets `packaging/release_build.py` builds (the DMG, the curl-path files,
`release-pins.json`, `release-provenance.json`, the corresponding-source archive and `SHA256SUMS`)
and the Claude Code plugin's copy of those pins (section 4). The release build assembles no
application identity by default; shipping one is the owner's separate decision
([owner checklist](plans/0.53-signing-owner-checklist.md), section 4), and a private bundle that
carries the owner's identity is never part of the public tree or of the release artifacts.

**Publication hold.** Building, signing and notarizing publish nothing. Assets are uploaded only when
Gpub (section 8) names them. While the release manifest says `publication: unreleased` (the build
writes that until the corresponding source is complete), the pins say so too: the plugin's
`bin/install` refuses with `release_not_published` and the documented curl recipe stops before running
anything.

## 1. Version sites

The release version appears in these places. A version-consistency test
(`tests/test_version_consistency.py`) keeps them equal; this list is the human-readable mirror,
and the test is the authority if the two differ.

| Site | What it holds |
| --- | --- |
| `src/telegram_search_mcp/__init__.py` | `__version__`, the single source for the package version (`pyproject.toml` reads it) |
| `packaging/launcher.py` | the bundle admission literal (`release_version`) and the help text |
| `tests/test_bundle_archive.py`, `tests/test_bundle_install.py` | the synthetic release fixtures |
| `tests/test_bundle_upgrade.py` | the change harness's default kit version (`build(version=…)`) and that kit's `__version__` line; `tests/test_account_default_goldens.py` derives its version line from that default |
| `tests/test_adopt_legacy.py`, `tests/test_bundle_late_refusal.py`, `tests/test_capability_profiles.py` | the `expected_package_version` line a change writes |
| `skills/telegram-search/SKILL.md`, `skills/telegram-setup/SKILL.md` | the "paired with" version line of each skill |
| `README.md`, `CHANGELOG.md` | the status paragraph and the top changelog heading |
| `plugins/telegram-local/.claude-plugin/plugin.json` | the plugin's `version` (`tests/test_claude_plugin.py` requires `__version__`) |
| `plugins/telegram-local/release-pins.json` | its `version`, equal to `plugin.json`'s: the unreleased placeholder during development, the build's own file in the release commit (section 4) |
| `docs/install.md`, `docs/reference.md`, `docs/roadmap.md`, `docs/security.md`, `docs/licensing.md`, `docs/third-party-notices.md` | candidate or release wording and the `expected_package_version` examples |
| `docs/release-notes-<version>.md` | the notes of this release; new for every release |

A new `docs/release-notes-<version>.md` and `packaging/constraints-<version>.txt` must also be
listed in `pyproject.toml` (`[tool.setuptools.data-files]`) and `MANIFEST.in`, as this document and
`packaging/release_gate.py` are; the gate refuses an sdist that misses a tracked public file. To
find stragglers: `git grep -n -F '<previous dev version>'`.

That grep also lists semantic floors, which are not version sites and which a bump leaves alone:
`_ACCOUNTS_FIRST` in `bundle_install.py` (the first release that knows account aliases) and the account
tests' fixture versions (`BASE_VERSION` in `tests/test_account_install.py`, `NEW_VERSION` and `THIRD` in
`tests/test_account_changes.py`, `ACCOUNTS_BASE` and `ACCOUNTS_NEWER` in `tests/test_bundle_kill_sweep.py`,
and the version lines those tests write), each of which installs its kits under that very version
whatever the package version is, and the `POLICY` bytes of `tests/test_account_launcher.py`, copied and
compared as they are. Each carries a comment saying so.

### Versions

Every version is one grammar: `X.Y.Z`, optionally followed by `aN`, `bN` or `rcN`, optionally
followed by `.devN`, where each number is `0` or has no leading zero and at most six digits
(`0.53.0`, `0.53.0rc2`, `0.53.0.dev0`, `0.53.0rc1.dev2`; never `0.53`, `0.53.0.post1` or
`0.53.0rc01`). The installer orders versions as PEP 440 does. Every site that admits a version holds
exactly this grammar: the native seed (`packaging/bootstrap_verifier.c`), the plugin's `bin/install`,
`packaging/release_pins.py`, the archive index (`release_archive.py`), the installer's order
(`bundle_install.py`), the launcher generator (`packaging/prepare_payload.py`) and the gate's project
version (`packaging/release_gate.py`). A release tag is `v` and a release or `rcN` candidate version
(`v0.53.0`, `v0.53.0rc2`): the gate and `packaging/release_build.py` refuse `aN`, `bN` and `.devN`
tags. `tests/test_version_grammar.py` holds every site, the compiled seed included, to one table of
admitted and refused versions (`tests/version_grammar.py`).

`expected_package_version` in a user's `runtime.toml` is a per-installation pin, not a version
site; see section 9. The tests of the frozen 0.51.0 harness (`tests/test_release_*.py`) keep their
0.51.0 fixtures on purpose and are not version sites.

## 2. Pre-cut conditions

All must hold before the gate runs; the release commit is the commit that holds them. A release with
assets has two such commits, the build commit and its release commit (section 4), and each one is
gated.

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
4. **Privacy guard green.** `tests/test_public_tree_privacy.py` passes (section 5).
5. **Versions agree.** `tests/test_version_consistency.py` passes with the release version.
6. **Documents ready.** `CHANGELOG.md`, `docs/release-notes-<version>.md` and `docs/roadmap.md`
   follow section 6, and every unmet gate bullet is disclosed in the notes.
7. **Pins reviewed.** If a dependency pin or `packaging/tdlib-lock.json` changed since the
   previous tag, `docs/third-party-notices.md`, `docs/licensing.md` and `docs/licenses/` match, and
   `packaging/constraints-<version>.txt` pins the tested closure with hashes.

Author and committer identity and per-commit history findings are not pre-cut conditions: the
gate reports them (section 5) and publication waits for the owner's history decision when the
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
  members against the tracked files, runs the privacy scan of section 5 (a hit refuses before the
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
- **Bundle stage (0.53 and later).** With `--bundle-native-directory <prepare_native.py output>` and
  `--bundle-cache <private download cache>` (both or neither, on `run` and again on `verify`), `run`
  also builds the bundle after the suite with the commit's own `packaging/release_build.py` in ad hoc
  dry-run mode, offline from the cached pinned inputs, into `<dir>/bundle`, runs its `check`, and
  records the hashes under `bundle`. The repository's `HEAD` must be the gated commit. `verify`
  re-derives the recorded hashes from disk and rebuilds; the rebuild must reproduce every hash except
  the DMG's and those that record it. Refusals: `bundle_inputs_invalid`, `bundle_commit_mismatch`,
  `bundle_failed`, `bundle_mismatch`. A release with assets runs the gate with this stage.

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
| `history_findings` | the per-commit history report of section 5: `commits`, `files` and `lines` scanned, the `total` and the first 2000 `findings` as `<12-hex commit>:<path>:<line>:<rule>`; a report, not a refusal |
| `identities` | `commits` in the range and each author, committer and co-author with role, `class` (`noreply` or `direct`) and commit count |
| `tests` | `tests`, `passed`, `failed`, `errors`, `skipped`, the `skips` with reasons, the last log line (`summary`), and `junit`, `log` with their SHA-256 |
| `bundle` | only with the bundle stage: `mode` (`adhoc-dry-run`), `source_commit`, and the sha256 of the kit's `archive`, `index`, `seed`, `pins` and `kit_sums`, the `source_archive`, the `dmg`, the `assets_sums` and the `build_evidence` (`release-build.json`) |

## 4. Release assets, signing and the release commit (0.53 and later)

A release that ships the DMG, the curl-path assets and the plugin's pins is cut in two commits. The
build commit holds the version and everything else of the release. Its release commit, the build
commit's only child, changes exactly one file: the plugin's `release-pins.json`, a byte-for-byte copy of
the signed build's own file. The tag goes on the release commit. That committed copy at the tag is the
trust root of the curl and plugin paths (see the [install guide](install.md#publisher-team-id-and-what-binds-what)),
never the copy uploaded next to the assets. The pins cannot live in the build commit itself, because
the archive they pin records that commit.

1. **Build commit.** The pre-cut conditions hold (section 2) and the gate with the bundle stage
   passes `run` and `verify` (section 3).
2. **Ad hoc dry run and the signed build** on the build commit, from a clean checkout, by the owner
   ([owner checklist](plans/0.53-signing-owner-checklist.md), section 5): `release_build.py build
   --dry-run` and `check`, then the Developer ID build without `--tag` (the tag is not on this commit)
   and its `check`. The build signs the payload and the seed, runs the kit's own seed `prepare` on the
   kit's own index and archive as `bootstrap.sh` runs it (`kit_failed` unless it answers verified for
   every indexed file; the unpacked copy is removed), checks every Mach-O in the kit, notarizes the payload zip and the DMG and staples the DMG; the output directory holds `kit/`,
   `assets/` and `release-build.json`. Its `kit/release-pins.json` has the one schema the plugin's
   `bin/install` reads (`packaging/release_pins.py`).
3. **Release commit.** Copy the file and commit only it, with the public identity:

   ```bash
   cp <out>/kit/release-pins.json plugins/telegram-local/release-pins.json
   git add plugins/telegram-local/release-pins.json
   git commit -m "<version>: release pins"
   PYTHONPATH=src .venv/bin/python packaging/release_build.py release-commit --output <out>
   ```

   `release-commit` exits 0 only when `HEAD` is clean, its only parent is the build's source commit
   (it re-runs `check` against that commit), it changes exactly the plugin's `release-pins.json`, that
   file equals the kit's byte for byte and `plugin.json` names the same version. A dry-run output is
   refused (`release_commit_invalid`): an ad hoc kit never becomes the plugin's pins.
4. **Gate the release commit** with the bundle stage (section 3); any later commit is a new release
   commit. The suite there checks that the committed pins have the schema and the Team ID of the
   reviewed native pin table (`tests/test_release_pins.py`).
5. **Tag the release commit** (section 7), then run `release-commit --tag v<version>` once more.
6. **Assets** are uploaded only under Gpub (section 8), to the GitHub release of that tag, exactly the
   files in `<out>/assets/`, each compared with `<out>/assets/SHA256SUMS` and `release-build.json`.

The publication hold above applies at every step. Upgrades of an installed bundle and their rollback
are the installer's (section 9 and section 10); the owner's own installation is migrated with
`--adopt-legacy` only in the owner session ([install guide](install.md#adopting-entries-the-installer-does-not-own---adopt-legacy)).

## 5. Privacy and identity

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
history remediation is the owner's decision, taken before publication (Gpub, section 8).

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

## 6. Changelog, release notes and roadmap

- **`CHANGELOG.md`**, newest first. The development heading `## <version>.dev0 — unreleased`
  becomes `## <version> — <YYYY-MM-DD>` in the release commit. Entries describe behaviour users
  can observe, mark owner decisions, and separate synthetic from real evidence.
- **`docs/release-notes-<version>.md`**, modelled on [the 0.51.0 notes](release-notes-0.51.0.md):
  the title `# <version> — <date>`, the navigation line, then Available behavior, Corrections
  included, Accepted scenarios (what was exercised, by which consumer, with real or synthetic
  providers), Known limits (including every unmet gate bullet of the release plan), and License
  and public identifiers. Include the upgrade note of section 9. Frozen after the tag, except
  for broken links.
- **`docs/roadmap.md`**: update the "Updated" date, move the candidate section into the completed
  slices, refresh the status rows, and link this document. Technical acceptance of a slice does
  not complete the plan (see "Maintaining this record" there).

## 7. Tag

Tag only after `verify` passes, and only locally. Tags are annotated, never lightweight, and
unsigned like `v0.51.0`: the name is `v<version>` for a release (`v<version>rcN` for a candidate),
the first message line is `<version>: <short theme>`, the tagger is the public identity. A release
with assets tags its release commit (section 4), never the build commit.

```bash
git tag -a v<version> -m "<version>: <short theme>" <sha>
git cat-file -t v<version>                       # tag
git for-each-ref refs/tags/v<version> --format='%(taggername) <%(taggeremail)> %(object)'
git rev-parse v<version>^{commit}                # equals the commit in evidence.json
```

## 8. Publication (Gpub)

Gpub is an explicit owner authorization naming the refs and artifacts to publish. Until it is
given, nothing is pushed to `main` and no tag leaves the machine; working branches may be pushed
as backups. It also needs the owner's decision on the history and identity question of section 5,
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
wheel, upload exactly the files `run` produced and compare their SHA-256 with the evidence. If it
names the release assets (section 4), upload exactly the files of `<out>/assets/` to the GitHub release
of the tag and compare each with `<out>/assets/SHA256SUMS`; the plugin path then works only for pins
that say `published`. There is no package-index upload by default.

Remote post-checks:

```bash
git ls-remote origin refs/heads/main refs/tags/v<version> 'refs/tags/v<version>^{}'
git clone --branch v<version> <remote-url> <fresh-dir-outside-tmp>
git -C <fresh-dir-outside-tmp> rev-parse HEAD 'HEAD^{tree}'   # commit and tree equal the evidence
```

`main` and the peeled tag must both equal the released commit, and the clone's tree hash must equal
the one in the evidence.

## 9. Upgrade note: `runtime.toml` pins

A broker and proxy refuse (`config_stale`) when the policy file pins another package version. A 0.53
or later bundle upgrade (a newer kit's `Install.command`, `bootstrap.sh` or the plugin's
`bin/install`) and `rollback` rewrite only that line themselves, after the pending-send gate
([install guide](install.md#upgrade)). For every other route, the release notes tell installed users:
after upgrading, set `expected_package_version` in
`~/Library/Application Support/TelegramSearchMCP/runtime.toml` to the new version, keep
`expected_contract_version` and the other lines, and keep the modes (`0700` directory, `0600` file). A policy written by `connect` pins the version that wrote it. A running
client session keeps its old proxy and is refused until its MCP server restarts; clients refresh
their tool listing after an upgrade. Never overwrite an existing policy blindly; see "Enabling more
capabilities" in [install.md](install.md) and the policy example in [reference.md](reference.md).

## 10. Rollback

Installed bundle (0.53 and later): the installed launcher's `rollback` returns to the release before
the last upgrade while no newer state exists, and a repeat changes nothing
([install guide](install.md#rollback)). Other installed runtimes: follow "Rollback" in
[security.md](security.md#rollback). Before an upgrade,
save the previous runtime, the owner-only plist, the consumer config and the installed skill in a
private directory outside the source tree; restore them and re-bootstrap only the validated
LaunchAgent. It leaves TDLib session data, credentials and historical data in place. The security
document also records the limit that matters across the 0.52 line: once a newer version has written
session state, an older binary refuses it, so a rollback needs the saved pre-upgrade files.

Published release: a pushed tag is not retracted. Fix forward with a new patch version through this
checklist; deleting a remote tag or branch is a separate owner-authorized action and does not remove
the objects.

## 11. After the release

Open the next development line in one commit by the public identity, on top of the tagged commit
(the tag is never moved):

1. Set `__version__` to the next development version (`<next>.dev0`) and update the version
   sites of section 1 that carry a development literal. The plugin's `release-pins.json` goes back to
   the unreleased placeholder (all-zero hashes and sizes, `"publication": "unreleased"`, `"team_id": ""`)
   with the new version, so that `bin/install` refuses until the next release commit.
2. Add `## <next>.dev0 — unreleased` on top of `CHANGELOG.md`.
3. Point the README status and `docs/roadmap.md` at the new candidate.
4. Run the full suite as in section 2; the version-consistency test must pass.

Release evidence is never edited: a corrected release is a new commit, a new `run` and a new tag.

### Consumer matrix (from the 0.54 candidate; every tool, the Inspector and the agent CLIs since the final acceptance)

```bash
.venv/bin/python packaging/release_consumer_matrix.py inspector-fetch --node <node 22 or later> --cache <dir>
.venv/bin/python packaging/release_consumer_matrix.py run --commit <sha> --receipt <absolute path> \
    --wheelhouse <gate dir>/wheelhouse --toolhouse <gate dir>/toolhouse --node <node> --inspector-cache <dir> \
    --codex <codex> --codex-home <Codex home> [--codex-model <model>] --claude <claude> \
    --agent-evidence <new owner-only dir>
.venv/bin/python packaging/release_consumer_matrix.py check --receipt <path> --commit <release sha> \
    --wheelhouse <gate dir>/wheelhouse --agent-evidence <the same dir>
```

`run` installs the wheel built from `git archive <sha>` with the pinned closure in a fresh venv and drives
every synthetic cell of the [consumer matrix](plans/final-consumer-matrix-spec.md) through the official SDK
against synthetic brokers, the installed bundle installer's install, upgrade, rollback and uninstall over a
synthetic home, the MCP Inspector CLI column (the pinned `packaging/inspector` lock, installed offline from
the cache `inspector-fetch` filled once) and the Codex and Claude Code columns: each agent cell gives the real
CLI one fixed request with a temporary configuration home whose only MCP server is the installed proxy, against a
synthetic broker of its own, and keeps the broker's transcript, the agent's events, its answer and a run record in
`--agent-evidence`. An agent run uses the agent's account and costs its usage. The receipt, `--workdir` and
`--agent-evidence` must lie outside the repository and any git work tree. Codex reads a copy of `auth.json` from
the Codex home `--codex-home` names (required with `--codex`). The one write outside the run's own directories:
when Codex refreshed its tokens during the run, the harness puts the refreshed `auth.json` back into that home
(atomically, owner-only), and only when the file there is still byte-identical to the copy and the refreshed file
is a JSON object with the same keys whose token fields are still non-empty; otherwise the file there stays as it
was. Either way a fixed line names the outcome (`returned`, `kept`, `invalid` or `failed`); the credential is never
printed. Claude Code logs in through its default configuration location (on macOS the Keychain, found through
`HOME` and `USER`) and loads nothing else of it: no setting source (user, project or local settings, hooks,
plugins), no auto memory, skill or command, only the temporary `--mcp-config` server (`--strict-mcp-config`), no
built-in tool and no transcript, from an empty working directory. The harness refuses the run
(`client_config_changed`) when the size, mtime or sha256 of `~/.claude.json`, `~/.claude/settings.json` or
`~/.claude/settings.local.json`, or the names in `~/.claude/projects`, differ after the version check or after any
cell. SIGTERM, SIGHUP and Ctrl-C stop the agent and the brokers and remove the temporary homes before the harness
exits. It refuses a schema the cells were not written for and writes the receipt only when every cell passed;
without Node or the cache the receipt records the Inspector column `unavailable` with its reason, and without the
CLI, a login or the Codex `auth.json` an agent column likewise (a login lost after a column's first cell fails the
run instead). `check --commit <release sha>` (the full sha) refuses a receipt of any other commit, rebuilds the
wheel and the schema census of the recorded commit, judges every passed agent cell again from the kept evidence
and refuses any difference; its verified line names the receipt's commit and wheel sha256. The receipt is
`acceptance: false`: owner and real-account cells stay `pending` and need their own records.

## Checklist

| Step | Command or file |
| --- | --- |
| Version sites equal | `tests/test_version_consistency.py` |
| Clean tree, release commit | `git status --porcelain`, `git rev-parse HEAD` |
| Full suite, isolated HOME, `build==1.3.0` and `setuptools==80.9.0` installed, 0 skipped | command in section 2 |
| Privacy guard | `tests/test_public_tree_privacy.py` |
| Changelog, notes, roadmap | `CHANGELOG.md`, `docs/release-notes-<version>.md`, `docs/roadmap.md` |
| Package data | `pyproject.toml`, `MANIFEST.in` |
| Gate | `.venv/bin/python packaging/release_gate.py run`, then `verify` (with the bundle stage for a release with assets) |
| Consumer matrix (synthetic, Inspector and agent cells) | `.venv/bin/python packaging/release_consumer_matrix.py run`, then `check --commit <release sha>` |
| Assets: dry run, signed build, release commit, its gate | section 4; [owner checklist](plans/0.53-signing-owner-checklist.md) |
| Identity report (not a pre-cut condition) | `identities` in `evidence.json`; owner decision before Gpub |
| History report (not a pre-cut condition) | `history_findings.total` in `evidence.json` is 0 or the owner's acceptance is recorded before Gpub |
| Tag | `git tag -a v<version> -m ...` (the release commit for a release with assets) |
| Gpub with remote pre and post checks | section 8 |
| Pin note for users | section 9, in the release notes |
| Rollback | `docs/security.md#rollback` |
| Dev bump | section 11 |
