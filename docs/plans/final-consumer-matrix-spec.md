# Final acceptance — consumer matrix (F18) — spec

**Status:** lead spec, 25.10.2026, written under the owner's standing delegation. Every decision below is a
**lead decision, owner review pending** (see the [completion plan](completion-plan.md)).
**Parent:** [`final-acceptance-spec.md`](final-acceptance-spec.md), work item F18, criteria 1 and 2.
**Extends:** [`0.54-consumer-matrix-spec.md`](0.54-consumer-matrix-spec.md): its columns, provider modes,
evidence forms and decisions still hold unless this spec says otherwise. **Tasks and evidence:**
[`final-acceptance-spec.tasks.md`](final-acceptance-spec.tasks.md).
**Harness:** `packaging/release_consumer_matrix.py` (receipt `tgmcp-consumer-matrix/v4` since part 2; v3 in part 1).

## 1. One sentence

The matrix covers every tool through 0.58 (64 tools): the harness drives 55 synthetic cells through the
official SDK (48 since part 1, and since the final audit seven more: the Boolean search form and the sender,
direction and topic filters, a synthetic XLSX and PPTX, a styled caption and the three 0.58 reply sources
prepared, and the typed text-send refusal), four cells through the MCP Inspector's scripted CLI mode, the
installed bundle installer's upgrade, rollback and uninstall paths and nine cells through each real agent CLI
(Codex, Claude Code), all against synthetic brokers and homes from one `git archive` wheel, and writes a receipt
bound to the commit, the wheel hash, the schema fingerprint the cells were written for, the harness, the release
gate and, for the agent cells, the digests of the evidence `check` judges again. The owner cells stay outside the receipt.

## 2. Columns

| Client | Synthetic broker | Who | Evidence |
| --- | --- | --- | --- |
| Official MCP SDK (`mcp` 2.2.0) | every synthetic cell of section 5 | harness | receipt `cells` |
| MCP Inspector CLI (`@modelcontextprotocol/inspector-cli` 1.0.2) | the four cells of section 7 | harness | receipt `inspector` |
| Codex (`codex exec`) | the nine agent cells of section 7a | harness (part 2) | receipt `agents.codex` and the kept evidence |
| Claude Code (`claude -p`) | the same cells, when the CLI is logged in (its default configuration, for the login only) | harness (part 2) | receipt `agents.claude_code` and the kept evidence |
| Claude Desktop, Claude Code plugin | not applicable (registration; the bundle session covers Desktop's entry) | owner | owner record |

The receipt's `owner_cells` drop `inspector_cli.synthetic` (part 1) and `codex.synthetic` and
`claude_code.synthetic` (part 2), now the harness's columns, and keep the other seven pending.

## 3. The synthetic account (additions to 0.54)

- Chat 91001 holds three messages: 55 (incoming text with a hashtag), 56 (the account's own text, editable
  and deletable for everyone) and 57 (the account's own document with a caption). Chat 91002 holds one reply
  source per 0.58 reply family (60–68: styled, mention, URL, email, phone, bank card, text link, custom
  emoji, cashtag). Every message carries the state fields the forward, edit and reader projections require;
  `getMessageProperties` answers the permissions those flows read.
- `getChatHistory` follows `td_api.tl`: from exactly `from_message_id` (0: the last message) with `-offset`
  newer messages before it. `searchChatMessages` and `searchMessages` match the query case-insensitively in a
  text or caption and answer in one page; a media filter finds nothing. `getUser` names every synthetic peer;
  `getChatListsToAddChat` offers the Archive list.
- Final audit additions (section 5a): chat 91002 also holds 69, a document whose caption `Matrix caption link`
  carries a text link (the 0.58 media TextUrl reply source, v24), and 73, a 30-second voice note whose caption
  `Matrix at 0:05` marks 0:05 as a media timestamp (the MediaTimestamp reply source, v28), each with the complete
  file shape a reply's media projection reads; a forum supergroup -91003 (supergroup 91003, `getSupergroup`
  answers `is_forum`, `getForumTopic` topics 41 and 42), outside the Main list, holds 70 (incoming, topic 41), 71
  (incoming, topic 42) and 72 (the account's own, topic 41). `searchChatMessages` keeps only its `sender_id` and `topic_id` when given,
  as TDLib does; the first chat and the forum have a history; chat 91002's history stays empty and
  `searchMessages` still searches the first chat only.
- Every provider-changing request (`sendMessage`, `sendMessageAlbum`, `forwardMessages`, `addChatToList`,
  `editMessageText`, `editMessageCaption`, `pinChatMessage`, `unpinChatMessage`, `deleteMessages`) is logged;
  `sendMessage` then loses its transport (outcome unknown), every other one answers an error. "No dispatch"
  means none of them in the broker log.
- The scripted owner renders every dialog and records its family (from the preview the broker passes) and
  verb; it declines, approves only the outcome probe, and lets the timeout probe's dialog time out
  (`approval.timeout`). `--real-dialog` still shows the real dialog for the denial probe only.

## 4. Sessions

Each session has its own short owner-only HOME under `/private/tmp`; the sessions run at once. The agent cells
run one at a time after them, each with a new HOME (`/private/tmp/tga.*`, short enough for the alias's socket).

| Session | Policy | Broker |
| --- | --- | --- |
| `full` | every known capability | authorized synthetic account, scripted owner |
| `restricted` | `artifacts`, `read`, `reply_artifact_send`, `reply_text_send`, `send` | authorized synthetic account |
| `stale` | every capability, a pinned version that is not loaded | none |
| `offline` | every capability | none |
| `unauthorized` | every capability | synthetic account in `authorizationStateWaitPhoneNumber` |
| `closed` | no capability | none (every guard refuses before the provider) |
| `bundle` | none | none: the installed `bundle_install` module over a synthetic home |
| `inspector` | every capability | authorized synthetic account, always the scripted owner |
| `listing` (check only) | every capability | none |
| agent `full` (one per cell) | every capability | authorized synthetic account, scripted owner |
| agent `restricted` (the refusal cell) | the restricted policy above | authorized synthetic account, scripted owner |
| agent `alias` (the alias cell) | every capability, for the default and for alias `work` | the alias's broker (`broker --account work`): the registry and the alias root as `account-add` leaves them |

## 5. The synthetic cells (55)

`n` is the frozen check count (`CELL_CHECKS`); a cell passes only with exactly that many checks.

| Cell | Session | n | Passes when |
| --- | --- | --- | --- |
| `handshake.2024-11-05` … `handshake.2025-11-25` (4) | full | 6 each | an initialize offering exactly that revision negotiates it; server name and version, `tools/list` equal to the census, `_manifest(check_broker=true)` compatible |
| `handshake.2026-07-28` | full | 6 | `server/discover` negotiates it, with the same checks |
| `census.tools` | full | 4 | names equal the census, unique, titled, object input schemas |
| `census.fingerprint` | full | 7 | `_manifest` fingerprint, versions, generation, capabilities, registered and available tools |
| `census.prompts` | full | 12 | the closed prompt set renders, quotes the request and grants nothing |
| `output_schemas` | all | 3 | results were validated, none failed its listed `outputSchema`, and every census tool returned at least one |
| `tools.every_tool` | full | 64 | each tool, in `EVERY_TOOL` order, answers its probe with its expected structured status (section 5.1) |
| `refusal.invalid_arguments` | full | 10 | schema violations return the fixed input-free text |
| `refusal.invalid_arguments_hook` | full | 2 | the strict `search_correspondence` hooks return the same text |
| `refusal.invalid_arguments_every_tool` | full | 65 | the listed required parameters equal `TOOL_REQUIRED`; one undeclared key on each tool returns `invalid_arguments: missing <required>; 1 unexpected argument(s)` |
| `refusal.unknown_tool` | full | 1 | `unknown_tool (refresh tools/list)` |
| `refusal.prompt_unavailable`, `refusal.prompt_arguments` | full | 1, 2 | fixed protocol errors |
| `refusal.client_schema_mismatch` | full | 4 | a wrong consumer fingerprint blocks tools until a matching recheck |
| `refusal.capability_disabled` | restricted | 5 | `read_history`, `list_chats`, `prepare_forward_send` refuse naming their capability and profile |
| `refusal.guard_profiles` | restricted | 33 | the 31 disabled tools are exactly the unlisted ones and each refuses naming exactly its missing capabilities and the one profile that unlocks them (`readers`, `forward`, `video`, `albums`, `archive_edit`, `pin`, `delete`); a `selection_id` adds `retained_selections` |
| `refusal.reply_families` | restricted | 22 | a reply to each family source returns `capability_disabled` with exactly `reply_formatted_targets` and the family's capability; styled reply text needs `formatted_text_send`, a styled reply caption `formatted_caption_send`; no draft |
| `refusal.text_options` | restricted | 5 | a styled `prepare_text_send` returns `capability_disabled` with exactly `formatted_text_send`, a styled topic one with `formatted_text_send` and `topic_text_send`, the fixed detail and `next_action: enable_capability`; no draft, dialog or dispatch (section 5a) |
| `refusal.guard_every_tool` | closed | 65 | only `_manifest` and `check_connection` are listed; each of the 60 gated tools refuses with its full requirement and hint (`owner enables profile …` or `owner must enable in runtime.toml`); the three upload-authority tools answer `unavailable` |
| `refusal.prompt_policy`, `refusal.config_stale` | restricted, stale | 2, 3 | as in 0.54 |
| `image.png`, `image.pdf_page` | full | 6, 5 | as in 0.54 |
| `image.attachment_page` | full | 5 | `read_attachment_page` of page 2 of a two-page PDF with `render_pages` returns that page's text and one `image/png` block |
| `cursor.list_chats` | full | 4 | 20 rows and a cursor, then the last 5 |
| `cursor.read_history`, `cursor.search_messages` | full | 6 each | `limit` 1 pages 57, 56, 55 by cursor to the end; a replayed cursor returns `invalid_cursor`, no rows, `restart_listing` |
| `cursor.list_drafts` | full | 5 | two owned drafts page one at a time with `after_draft_id`, then `has_more` false |
| `search.boolean` | full | 6 | `search_messages` with `{all: [synthetic, own], none: [caption]}` returns 56, `{any: [source, caption]}` 57 and 55, `{any: [matrix, synthetic]}` 57, 56 and 55 once each from its two branches (3 scanned each); every page `partial` (`provider_end_unverified`) under the local matching semantics, every result a complete rehydrated match |
| `search.filters` | full | 7 | the sender filter (the peer) returns 55; `direction` outgoing 57 and 56, incoming 55; the forum topic 41 returns 72 and 70 (not 71), each in that topic; topic, own sender and outgoing together 72; the Boolean form `{all: [forum], none: [own]}` in topic 41, incoming, 70 |
| `documents.xlsx` | full | 6 | `read_spreadsheet` of a synthetic XLSX lists the catalog (`Matrix` visible, `Hidden` hidden); `A1:B2` of sheet 1 is complete: an inline string, the number 2, the formula `B1*2` as data with its cached 4, an empty cell; `A1` of the hidden sheet its text |
| `documents.pptx` | full | 5 | `read_presentation` of a synthetic PPTX lists the catalog (slide 1 with notes, slide 2 hidden without); slide 1 with `include_notes` returns its text and notes, `selection_complete`, never `full_content_complete`; slide 2 its text and no notes |
| `recovery.*` (7) | as in 0.54 | 4, 2, 8, 5, 3, 4, 3 | one cell per value of the closed `next_action` map |
| `caps.manifest`, `caps.input_bounds` | full | 8, 6 | as in 0.54 |
| `caps.input_bounds_058` | full | 13 | one past each cap of the tools through 0.58 (hypotheses 6, message ids 21, album items 11 and 1, album caption 1,025, edit text 4,097, new caption 1,025, 33 styles, `list_drafts` limit 51, `max_depth` 11, 21 retained anchors, 6 search targets, `max_cells` 201) is refused with the fixed text |
| `prepare.styled_caption` | full | 4 | `prepare_artifact_send` of a PNG with a bold caption style is prepared with those `outgoing_entities` and a `preview_sha256`, which `get_draft` shows unchanged; `prepare_reply_artifact_send` with the same style is prepared; no dialog, no dispatch |
| `prepare.reply_sources_058` | full | 7 | a reply to 69 (media caption with a text link, v24), to 67 (custom emoji, v25) and to 73 (voice note with a media timestamp, v28) is prepared with its exact target and text; the media evidence is the record naming the link's URL on `link` (one inert JSON layer) and the timestamp's 5 seconds on `0:05`, the custom emoji evidence names its id; no dialog, no dispatch |
| `approval.declined_flag`, `approval.denial` | full | 5, 8 (+1 real dialog) | as in 0.54 |
| `approval.timeout` | full | 6 | the timeout probe's dialog times out: `timeout` with its fixed detail, one rendered text dialog, no dispatch, the draft still pending |
| `approval.denial_every_family` | full | 34 | artifact, reply text, reply artifact, forward, video, album, archive, text edit, caption edit, pin and delete: each is prepared, approved by the caller, declined by one rendered dialog of its family and verb (`send`, `change` or `delete`); no dispatch at all |
| `bundle.install` | bundle | 8 | first install of an older synthetic release: `activation_required`, pointer, v2 receipt, Codex and Claude Desktop entries with neighbours kept, skills, no broker start, the session untouched |
| `bundle.upgrade` | bundle | 9 | upgrade to this package's release: `upgraded`/`restarted`, pointer, broker definition, fake launchd bootout then bootstrap, both client entries, the pin moved, the session untouched, the old release kept, `previous` recorded |
| `bundle.rollback` | bundle | 7 | back to the older release: `rolled_back`/`restarted`, pointer, definition, entries, skills, pin, session, the newer release kept |
| `bundle.uninstall` | bundle | 8 | `uninstalled` with everything owned removed (root, plist and job, Codex and Desktop entries, skills), neighbours and the whole session kept; a repeat answers `absent` |

Surface (`SURFACE_COUNTS`): 183 structured results validated (157 before the final audit cells), from all 64 tools.

### 5.1 Every tool

`EVERY_TOOL` fixes the order and the expected status of each probe; later probes name what earlier ones
returned (a draft, a handle, a result reference, an upload). Sends and applies use `approved=false`, so no
dialog opens. The statuses are those of the synthetic account: `discover_targets`, `read_history`,
`search_messages` and `search_chats` answer `partial` (the account is too small to prove the provider's end),
`get_attachment` of a text message, `analyze_media` of a PNG, `list_topics`/`read_topic_history` of a private
chat and `read_spreadsheet`/`read_presentation` of a CSV answer `unsupported`; every other tool reaches its
success status (`prepared`, `declined`, `pending`, `revised`, `retained`, `complete`, …).

### 5a. The final audit cells

The [final audit](final-audit.md) left F6, F9 and F13 partly met for want of an installed consumer calling the 0.51
forms and the 0.58 prepares under a policy that allows them, and recorded two findings of this matrix (section 10).
Seven cells close the agent side: `search.boolean` and `search.filters` (F6), `documents.xlsx` and
`documents.pptx` (F9), `prepare.styled_caption` and `prepare.reply_sources_058` (F13), all in the full session on
its legacy proxy before the approval cells, and `refusal.text_options` in the restricted session, which freezes the
typed `prepare_text_send` refusal (the second finding, fixed with the listed schema change
`0.59-text-send-capabilities`; `MATRIX_SCHEMA_FINGERPRINT` moved with it). The search cells run on the first chat's
three messages and the new forum, so no earlier cell's rows change; the documents are hand-authored OOXML packages
built by the harness byte for byte (stdlib `zipfile`, fixed timestamps), read through `create_local_artifact`; the
prepares never send. Statuses are those of the synthetic account (a search page `partial`, as in section 5.1).

## 6. The bundle session

The installed `telegram_search_mcp.bundle_install` runs in the driver process over a synthetic home (resolved,
no symlinked ancestor) with two synthetic admitted releases built as the installer tests build them (inert
bytes, a v1 lock), the Claude Code mode `skip`, a fake `launchctl` (print, bootout, bootstrap of the one
label), a pending-send probe answering `none` and `require_platform` patched as the installer tests patch it.
Every other process is refused for the duration of each step. tomlkit, which the bundle vendors as
`installer/tomlkit`, is the release gate's pinned wheel (`TOOLS`): taken from `--toolhouse` or downloaded with
its hash, checked, extracted and put on the session's path. The receipt records it under `installer`.

## 7. The Inspector column

- **Pin.** `packaging/inspector/package.json` depends on exactly `@modelcontextprotocol/inspector-cli`
  1.0.2; `package-lock.json` pins its whole tree (99 packages), each an npm registry tarball with a sha512
  integrity and no install script, resolved as of 04.10.2026. The harness checks the lock against
  `INSPECTOR_VERSION` and `INSPECTOR_INTEGRITY` and the receipt records the lock's sha256 (bound to the
  commit's lock by `check`).
- **Fetch once, run offline.** `inspector-fetch --node <node> --cache <dir>` fills an npm cache online with
  `npm ci --ignore-scripts` (npm verifies every integrity); `run --node <node> --inspector-cache <dir>`
  installs the commit's lock with `npm ci --offline --ignore-scripts` into the workdir, under a private HOME
  and empty npm configs, and checks the installed package.
- **Unavailable, never skipped.** Without `--node` (`node_not_given`), with a Node older than 22
  (`node_unsupported`), without the cache (`inspector_cache_missing`), with a lock that fails the pins
  (`inspector_lock_invalid`) or when the offline install fails (`inspector_install_failed`) the receipt records
  `{"status": "unavailable", "reason": …}` and the summary line says so. A failed Inspector cell refuses the
  run (`cells_failed`).
- **Cells.** `inspector.census` (5): `tools/list` names equal the census and every tool's input and output
  schema and title equal the official SDK's listing; `_manifest` is compatible with the census fingerprint.
  `inspector.read` (5): `list_chats` returns its first page of 20 with a cursor, valid under the Inspector's
  own listed `outputSchema`, its JSON text block equal to the structured content. `inspector.refusal` (2): the
  fixed `invalid_arguments: invalid scope` text, no structured content. `inspector.approval_denial` (8):
  prepare, send with `approved=true` and status in one session are prepared, `declined` with its fixed detail
  after one rendered dialog, still `pending`; a later CLI invocation's send of that draft answers `expired`
  without a dialog; nothing is dispatched.
- **One session for the approval cell.** The CLI binary makes one call per process, so each invocation is its
  own MCP session and broker client, and a draft belongs to the session that prepared it (0.57). The approval
  cell therefore drives the Inspector CLI's own client module (`connect`, `callTool`, `disconnect`,
  `createTransport` and the SDK `Client` the CLI uses) from a fixed script the harness writes beside the
  session, never into the package; census, read and refusal run the CLI binary itself
  (`build/index.js --method …`, from its build directory, where it finds its package.json).

## 7a. The agent columns (part 2)

- **Cells (per column, frozen `AGENT_CHECKS`).** Each cell is one fixed request (`AGENT_CELLS`, after a fixed
  preamble naming the server) given to the real CLI once:

  | Cell | Session | n | Decisive checks besides the five common ones |
  | --- | --- | --- | --- |
  | `agent.census` | full | 7 | a lone-handshake connection (the `_manifest(check_broker=true)` check); nothing changed |
  | `agent.read` | full | 7 | a read of chat 91001 returned its newest message (57); nothing changed |
  | `agent.search_cursor` | full | 9 | `search_messages` with `limit` 1 returned one row and a cursor; a later call carried an earlier `next_cursor`; 55, 56 and 57 were returned and a followed page ended without a cursor; nothing changed |
  | `agent.refusal` | restricted | 8 | a reply to the mention source 61 returned `capability_disabled` with exactly its `missing_capabilities` and `next_action: enable_capability`; no draft or selection of any kind (no workaround); no dispatch, no dialog |
  | `agent.approval_timeout` | full | 9 | the timeout probe was prepared; its approved send answered `timeout` with the fixed detail; exactly one rendered dialog, which timed out (no retry); no dispatch |
  | `agent.alias_server` | alias | 8 | the registry lists `work` pinned; the read returned the newest message; nothing changed |
  | `agent.album_prepare` | full | 8 | two artifacts made; one album of two items prepared for 91001; no approved send or apply, no dialog, no dispatch |
  | `agent.own_message_prepare` | full | 7 | an edit of own message 56 to the exact text prepared; nothing sent or applied |
  | `agent.family_reply` | full | 7 | a reply to 61 in 91002 with the exact text prepared; nothing sent |

  The five common checks: the agent exited 0 inside 420 s; its turn completed (Codex `turn.completed` and no
  `turn.failed`; Claude Code results that all succeeded); it used only the matrix server's tools (no shell, other
  MCP server or built-in tool; Claude Code's `ToolSearch` only loads a deferred tool's schema); the broker's header
  names this wheel's version and fingerprint (and the alias); an answer exists. The answer's wording is advisory
  (`AGENT_ADVISORY`, recorded in `advisory_failed`), as in the 0.54 conversation runner.
- **The server's transcript.** Every synthetic broker records, beside the provider requests and owner dialogs, a
  header (`version`, `schema_fingerprint`, `alias`), each frame a connection received (numbered by connection) and
  each dispatched operation with its payload and result or fixed error code. The alias cell adds the harness's
  registry postscript (alias names and which are pinned, never a user id). Decisions rest on this record.
- **Isolation.** Codex: a temporary HOME and a temporary `CODEX_HOME` holding a `config.toml` whose only MCP server
  is the installed proxy (`approval_policy = "never"`, `cli_auth_credentials_store = "file"`, features
  `shell_tool`, `browser_use`, `computer_use`, `apps`, `multi_agent`, `image_generation` and `plugins` off,
  `default_tools_approval_mode = "approve"`), run with `--strict-config --ephemeral --skip-git-repo-check --sandbox
  read-only` in an empty directory; the only file copied in is `auth.json` from the Codex home `--codex-home` names
  (required with `--codex`, never derived from `CODEX_HOME` or `~/.codex`). **Credential write-back**, the only
  write outside the run's own directories: when Codex refreshed its tokens in the private home, the refreshed
  `auth.json` replaces the one in that Codex home (atomically, owner-only) only when the file there is still
  byte-identical to the copy and the refreshed file is a JSON object with the same top-level keys (and the same
  `tokens` keys) whose non-empty string fields are still non-empty strings (`returned`); otherwise that file stays as
  it was (`kept` when it changed meanwhile, `invalid`, or `failed` when the write fails, with no temporary file
  left). A fixed line names any outcome but `unchanged`; the credential is never printed or kept elsewhere.
  Claude Code: the default configuration location for its login only (on macOS the Keychain, which Claude Code finds
  through `HOME` and `USER`, so these two are the caller's), with `--setting-sources ""` (no user, project or local
  settings: no hooks, plugins or settings-defined servers), `--settings '{"autoMemoryEnabled":false}'`,
  `--disable-slash-commands` (no skills or commands), `--strict-mcp-config` with a temporary `--mcp-config` outside
  the working directory, `--tools ""`, `--allowedTools mcp__<server>`, `--no-session-persistence`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_AUTOUPDATER=1`, in an empty working directory. The
  harness takes the size, mtime and sha256 of `~/.claude.json`, `~/.claude/settings.json` and
  `~/.claude/settings.local.json` and the names in `~/.claude/projects` before the column and refuses the run
  (`client_config_changed`) when any differs after the version check or after a cell. Both get besides only `PATH`,
  `TMPDIR`, `LANG`, the network proxy variables and, for Claude Code, `ANTHROPIC_API_KEY` or
  `CLAUDE_CODE_OAUTH_TOKEN` when the operator exported one. The temporary homes are deleted afterwards, also when
  the run is interrupted (section 8).
- **Retries and unavailability.** Only what the CLI itself reports as failing is classified: its stderr and its
  provider error events (Codex `error` events, `turn.failed` errors and `error` items; Claude Code API error
  messages and failed results), never the model's own messages or reasoning. A failure before any frame reached the
  broker is retried (three attempts) when those show a provider failure (`at capacity`, a rate limit, …). A login
  failure there on the column's first cell makes the column `unavailable` with `not_logged_in`; on a later cell it
  fails the column (the run refuses `agent_failed`), keeps every result so far and says so in a fixed line. The
  other fixed reasons: `cli_not_given`, `cli_unusable` (no `--version` of the CLI's form) and `auth_missing` (no
  regular `auth.json` in the Codex home).
- **Evidence.** `--agent-evidence <dir>` (new or empty, owner-only, outside any git work tree) keeps every attempt
  of a cell in `<column>/<cell>/attempt-<n>/`: `server.jsonl`, `events.jsonl`, `answer.txt` and `run.json`
  (commit, wheel sha256, version, fingerprint, the commit's MCP server name, column, cell, CLI version, model, exit,
  timeout, attempt) owner-only, beside the agent's `stderr.txt` (diagnostic, unbound). The receipt cell records its
  attempt count and binds the last attempt's four files by sha256; `check --agent-evidence` re-reads them, compares
  the run record's bindings (the attempt included), requires each earlier attempt kept, numbered and without any
  frame at its broker, and judges every cell again with the same function.

## 8. Receipt v4 and harness contract

```bash
.venv/bin/python packaging/release_consumer_matrix.py inspector-fetch --node <node> --cache <dir>
.venv/bin/python packaging/release_consumer_matrix.py run --commit <sha> --receipt <absolute path> \
    [--wheelhouse <dir>] [--toolhouse <dir>] [--node <node> --inspector-cache <dir>] [--workdir <dir>] [--real-dialog] \
    [--codex <path> --codex-home <dir> [--codex-model <model>]] [--claude <path> [--claude-model <model>]] \
    [--agent-evidence <dir>]
.venv/bin/python packaging/release_consumer_matrix.py check --receipt <path> [--commit <sha>] [--wheelhouse <dir>] \
    [--agent-evidence <dir>]
```

- v4 adds `agents`: per column either `{"status": "unavailable", "reason": …}` or the CLI's version, the model
  (`default` when none was given) and each cell as `{id, status, checks, advisory_failed, evidence, attempts}`, with
  `codex`, `codex_cells`, `claude_code` and `claude_code_cells` in the summary. A failed agent cell refuses the
  run (`agent_failed`), as a failed Inspector cell does. New fixed codes: `agent_invalid`, `agent_failed`,
  `evidence_invalid`, `evidence_missing`, `evidence_mismatch`, `client_config_changed`. A v3 receipt is refused.
- The F18 review fixes amended v4 before any release: the cell's `attempts`, `client_config_changed`, `run`'s
  refusal of `--codex` without `--codex-home` (`agent_invalid`) and of a receipt (`usage`), `--workdir`
  (`workdir_invalid`) or `--agent-evidence` (`evidence_invalid`) inside the repository or any git work tree, and
  interrupts: under `run` and `check`, SIGTERM and SIGHUP kill every running agent's session and raise through every
  cleanup (brokers and drivers stopped, temporary homes and the copied credential removed, a refreshed credential
  still returned), a repeat signal never cuts the cleanup short, and the command prints `interrupted` and exits
  with 128 + the signal. `check`'s verified line names the receipt's commit and wheel sha256; the release recipe
  passes `check --commit <release sha>` (a full sha), which refuses a receipt of any other commit.

- v3 adds `installer` (distribution, version, sha256 as the commit's gate pins it, toolhouse provided or
  downloaded) and `inspector` (the pass or unavailable form), adds `inspector` and `inspector_cells` to the
  summary and drops `inspector_cli.synthetic` from `owner_cells`. A v2 receipt is refused.
- **The schema is frozen.** `MATRIX_SCHEMA_FINGERPRINT` is the fingerprint these cells were written for (the
  0.58 surface; since the final acceptance with the typed `prepare_text_send` refusal, `85b80a45…b071`). `run`
  refuses another census before any session (`schema_mismatch`), `check_receipt` refuses a receipt with another
  fingerprint on its own, and `check` refuses a commit whose census differs. A deliberate
  schema change must update the cells, their counts, the tool tables and the constant together; a unit test
  holds the constant and every table to the source.
- Every v2 rule still holds (commit, wheel, census, closure, listing, harness and gate bindings; frozen
  counts; fixed refusal codes). New fixed codes: `toolhouse_invalid`, `installer_failed`, `inspector_invalid`,
  `cache_invalid`, `node_invalid`, `fetch_failed`.

## 9. Decisions and rejected options

Final audit cells (lead decisions, owner review pending):

- **The forum is a chat of its own, outside the Main list**, and the media TextUrl and MediaTimestamp sources are
  new messages of chat 91002: the first chat keeps its three messages, so the cursor, read and agent cells see
  the same rows, and `cursor.list_chats` the same 25 chats. Rejected: forum messages or a captioned document in
  the first chat (every cursor cell and the agent read cell would change) and a forum among the Main chats (the listing caps change).
- **The synthetic `searchChatMessages` applies `sender_id` and `topic_id`**, as TDLib does; the readers filter the
  rows again on the rehydrated message, so the cells prove both. The history of chat 91002 stays empty, as before.
- **The typed text-send refusal is frozen in the restricted session** (`refusal.text_options`) rather than in
  `refusal.reply_families`, whose count stays tied to the family table.
- **The agent columns are unchanged** (nine cells each); the lead runs them with the whole matrix at the release
  candidate.
- **The voice-note MediaTimestamp source is included** although the work item named only the media TextUrl and the
  custom emoji: the audit's F13 gap names all three 0.58 sources, and one more check in the same cell closes it.

Part 2 (agent columns):

- **A private Codex home with its own `config.toml`, not `--ignore-user-config` with `-c` overrides** (the
  conversation runner's form): the configuration home itself then names only the proxy, `--strict-config` refuses
  any key this Codex does not know, and nothing of the caller's Codex home but `auth.json` is read.
- **Refreshed Codex credentials go back.** Codex rotates its refresh token on use; a refresh in the private home
  would leave the caller's `auth.json` with a spent token. The harness returns the refreshed file atomically
  (owner-only) only when the caller's file is still the one it copied, and never prints or keeps it elsewhere.
  Rejected: a symlinked `auth.json` (Codex may replace it by rename) and refusing to run when a refresh might be
  due (Codex's refresh schedule is not documented).
- **Claude Code logs in through its default configuration and loads none of it** (F18 review; it replaces part
  2's "login only through an exported credential", under which the column never ran on a machine where `claude` is
  logged in). On macOS Claude Code keeps its login in the Keychain and finds it through `HOME` and `USER`; the
  harness never reads the Keychain itself. Checked on the release machine before the run: with an empty `HOME`
  (with or without `CLAUDE_CONFIG_DIR`) or without `USER`, `claude auth status` reports no login; with the caller's
  `HOME` and `USER` it reports the claude.ai login. A `claude -p` probe with the flags of section 7a reported no
  built-in tool, no MCP server, no skill or command, no memory path and only Claude Code's built-in plugins,
  answered that no CLAUDE.md, AGENTS.md or memory file was in its context, and left the guarded files and the
  project list unchanged; without `autoMemoryEnabled: false` it had created an empty auto-memory folder under
  `~/.claude/projects` (removed). Claude Code registers a running session in `~/.claude/sessions` and removes the
  entry when it exits; that runtime registry is not configuration and is not guarded. Rejected: `--bare` (never
  reads OAuth or the Keychain), a temporary HOME (the Keychain is not found), a copy of `~/.claude.json` (it loads
  the user's configuration) and an exported credential as the only way in (each run would need a long-lived token
  minted for it). An exported `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` still takes precedence.
- **The Codex home is named** (`--codex-home` required with `--codex`), so the operator names the file that may be
  written back; rejected: a default from `CODEX_HOME` or `~/.codex`. The write-back is validated and
  exception-safe (section 7a); rejected: writing back any changed bytes, which let a truncated file replace a
  working credential.
- **A login lost after the first cell fails the column** instead of making it `unavailable`: an unavailable column
  is an accepted release state, so it must never hide a failed cell or discard results.
- **Retry decisions read provider errors only, and every attempt is kept:** matching the model's own words could
  retry a cell or end its column on prose, and a retry used to delete the attempt it replaced.
- **Interrupts raise** (a handler that kills the agents' sessions, then raises once): rejected, killing the process
  groups alone, which leaves the temporary homes, the copied credential and an unreturned refresh behind.
- **Run outputs stay out of git work trees:** evidence and receipts carry machine paths, and an agent working
  directory inside a checkout would load its CLAUDE.md or AGENTS.md.
- **The broker records the transcript, not the proxy:** the installed proxy runs unchanged; every tool call that
  reaches the account passes the broker, and a refusal before it (an argument or guard refusal) leaves no draft.
- **The MCP server name is read from the commit's `product_identity.py`** (never imported, never a literal in
  `packaging/`, which the product identity guard forbids).
- **One request per cell, decided on the transcript**; tool choice is free (a read may use `read_history`,
  `read_messages`, `get_message_context` or a search) as long as the outcome is in the broker's record.
- **Rejected:** keeping the evidence in the receipt (transcripts are synthetic but large; the receipt stays under
  1 MiB) and a separate agent receipt (the agent cells would no longer be bound to the same wheel and census).

Part 1:

- **Inspector v1's CLI package, not v2.** Inspector 2.x (current `latest`) bundles a keyring module
  (`@napi-rs/keyring`, a native binary that can reach the Keychain), Vite, React and Ink, requires Node
  22.19 or later and still makes one call per invocation; the harness must never be able to touch the
  Keychain. The 1.x line still receives security fixes (`v1-latest`); 1.0.2 is the newest release older than
  two weeks (1.0.3 is from 21.10.2026). The meta package `@modelcontextprotocol/inspector` 1.x was rejected
  too: it adds the web client and proxy, irrelevant to scripted CLI mode.
- **A relay that keeps one proxy across CLI invocations was rejected:** the Inspector would no longer talk to
  the installed proxy itself, and a second `initialize` on one server stream is outside the protocol.
- **The schema fingerprint is frozen in the harness** rather than taken from the census alone: otherwise a
  schema change in the commit would pass run with cells that never saw it.
- **The restricted policy gains `reply_artifact_send`** so that styled reply captions reach their
  `formatted_caption_send` result; no 0.54 restricted cell changes.
- **A `closed` session** (no capability) proves every gated tool's full guard text; the restricted session
  proves the missing-subset texts and profile hints.
- **History follows the documented offsets** (`td_api.tl`); the readers handle both inclusive and exclusive
  providers, so the synthetic account uses the documented one.
- **The bundle session uses Claude Code mode `skip`:** the `claude` CLI fake belongs to the installer tests;
  Claude Desktop and Codex registrations, the service and the session are what the matrix holds.
- **Statuses of the synthetic account are frozen, not idealized:** `partial` and `unsupported` where the
  account is too small or the file is not a spreadsheet or presentation; the cell still proves the tool is
  listed, callable and schema-valid through the consumer.

## 10. Findings recorded for the audit (fixed in the final acceptance)

- **Retained selections of messages with entities.** `selection_projection.message_projection` accepts an
  entity only with exactly the keys `offset`, `length`, `type`, while TDLib's JSON writes every object with its
  `@type` (`"@type": "textEntity"`), which the forward projection's own check requires. On the synthetic
  account the hashtag message 55 forwards but `retain_selection` of it answers `selection_unavailable`; the
  selection tests use entities without `@type`. Not yet checked on the real account. The matrix retains a
  message without entities (56) and does not freeze this behaviour. **Fixed** in 2c87e08: the projection accepts
  exactly TDLib's shape (the `@type` key with that value, nothing else extra), the fixtures carry it, and broker tests
  retain, read and refuse after drift a message with TDLib-shaped entities.
- **`prepare_text_send` without `formatted_text_send`.** A styled text send under a policy without the
  capability answers `status: error` with `topic draft could not be prepared safely`, where the reply tools
  answer `capability_disabled` with `missing_capabilities`. **Fixed** in 12f53f6 at the broker, the proxy and
  through the SDK (a topic option too), a listed schema change; `refusal.text_options` freezes it (section 5a).

## 11. Seams for part 2 (agent cells), as they were used

- The synthetic broker is `release_consumer_matrix.py broker --profile authorized --approval scripted
  --package-root <site>` inside the installed venv, with the session HOME's `runtime.toml` from
  `write_policy`; an agent cell needs only the installed proxy path (`<venv>/bin/telegram-search-mcp`), that
  HOME, `PATH`, `TMPDIR` and `LC_ALL`, exactly as `proxy_parameters` builds them.
- The broker log (`<home>/matrix-broker.jsonl`) gives an agent cell's dispatch and dialog evidence
  (`owner_prompts`, `dispatches`), so an agent cell can be judged on the server's record as the conversation
  runner judges its demo transcript.
- `run_sessions(..., sessions=…, extra=…)` already starts any named session with its own broker and plan;
  an agent session would add its name to `session_layout` and an `AGENT_CELLS` column like `INSPECTOR_CELLS`
  (fixed ids, frozen counts, an `unavailable` form with fixed reasons such as `not_logged_in`), recorded in the
  receipt beside `inspector`.
- `tests/conversation_agent_run.py` already drives real `codex exec --ignore-user-config` and
  `claude -p --strict-mcp-config` with temporary configuration homes against the demo server; part 2 points
  the same override at the installed proxy and a matrix broker instead of `conversation_demo_server.py`.

## Result

(Recorded in the tasks file.)
