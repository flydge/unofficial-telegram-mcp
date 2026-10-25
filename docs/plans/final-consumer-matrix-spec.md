# Final acceptance — consumer matrix (F18) — spec

**Status:** lead spec, 25.10.2026, written under the owner's standing delegation. Every decision below is a
**lead decision, owner review pending** (see the [completion plan](completion-plan.md)).
**Parent:** [`final-acceptance-spec.md`](final-acceptance-spec.md), work item F18, criteria 1 and 2.
**Extends:** [`0.54-consumer-matrix-spec.md`](0.54-consumer-matrix-spec.md): its columns, provider modes,
evidence forms and decisions still hold unless this spec says otherwise. **Tasks and evidence:**
[`final-acceptance-spec.tasks.md`](final-acceptance-spec.tasks.md).
**Harness:** `packaging/release_consumer_matrix.py` (receipt `tgmcp-consumer-matrix/v3`).

## 1. One sentence

The matrix covers every tool through 0.58 (64 tools): the harness drives 48 synthetic cells through the
official SDK, four cells through the MCP Inspector's scripted CLI mode and the installed bundle installer's
upgrade, rollback and uninstall paths, all against synthetic brokers and homes from one `git archive` wheel,
and writes a receipt bound to the commit, the wheel hash, the schema fingerprint the cells were written for,
the harness and the release gate. The Codex and Claude Code agent cells (part 2) and the owner cells are
recorded outside the receipt.

## 2. Columns

| Client | Synthetic broker | Who | Evidence |
| --- | --- | --- | --- |
| Official MCP SDK (`mcp` 2.2.0) | every synthetic cell of section 5 | harness | receipt `cells` |
| MCP Inspector CLI (`@modelcontextprotocol/inspector-cli` 1.0.2) | the four cells of section 7 | harness | receipt `inspector` |
| Codex (`codex exec`) | agent cells | agent (part 2) | agent record |
| Claude Code (`claude -p`) | agent cells when logged in | agent (part 2) | agent record |
| Claude Desktop, Claude Code plugin | not applicable (registration; the bundle session covers Desktop's entry) | owner | owner record |

The receipt's `owner_cells` drop `inspector_cli.synthetic` (now the harness's column) and keep the other
nine pending.

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
- Every provider-changing request (`sendMessage`, `sendMessageAlbum`, `forwardMessages`, `addChatToList`,
  `editMessageText`, `editMessageCaption`, `pinChatMessage`, `unpinChatMessage`, `deleteMessages`) is logged;
  `sendMessage` then loses its transport (outcome unknown), every other one answers an error. "No dispatch"
  means none of them in the broker log.
- The scripted owner renders every dialog and records its family (from the preview the broker passes) and
  verb; it declines, approves only the outcome probe, and lets the timeout probe's dialog time out
  (`approval.timeout`). `--real-dialog` still shows the real dialog for the denial probe only.

## 4. Sessions

Each session has its own short owner-only HOME under `/private/tmp`; the sessions run at once.

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

## 5. The synthetic cells (48)

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
| `refusal.guard_every_tool` | closed | 65 | only `_manifest` and `check_connection` are listed; each of the 60 gated tools refuses with its full requirement and hint (`owner enables profile …` or `owner must enable in runtime.toml`); the three upload-authority tools answer `unavailable` |
| `refusal.prompt_policy`, `refusal.config_stale` | restricted, stale | 2, 3 | as in 0.54 |
| `image.png`, `image.pdf_page` | full | 6, 5 | as in 0.54 |
| `image.attachment_page` | full | 5 | `read_attachment_page` of page 2 of a two-page PDF with `render_pages` returns that page's text and one `image/png` block |
| `cursor.list_chats` | full | 4 | 20 rows and a cursor, then the last 5 |
| `cursor.read_history`, `cursor.search_messages` | full | 6 each | `limit` 1 pages 57, 56, 55 by cursor to the end; a replayed cursor returns `invalid_cursor`, no rows, `restart_listing` |
| `cursor.list_drafts` | full | 5 | two owned drafts page one at a time with `after_draft_id`, then `has_more` false |
| `recovery.*` (7) | as in 0.54 | 4, 2, 8, 5, 3, 4, 3 | one cell per value of the closed `next_action` map |
| `caps.manifest`, `caps.input_bounds` | full | 8, 6 | as in 0.54 |
| `caps.input_bounds_058` | full | 13 | one past each cap of the tools through 0.58 (hypotheses 6, message ids 21, album items 11 and 1, album caption 1,025, edit text 4,097, new caption 1,025, 33 styles, `list_drafts` limit 51, `max_depth` 11, 21 retained anchors, 6 search targets, `max_cells` 201) is refused with the fixed text |
| `approval.declined_flag`, `approval.denial` | full | 5, 8 (+1 real dialog) | as in 0.54 |
| `approval.timeout` | full | 6 | the timeout probe's dialog times out: `timeout` with its fixed detail, one rendered text dialog, no dispatch, the draft still pending |
| `approval.denial_every_family` | full | 34 | artifact, reply text, reply artifact, forward, video, album, archive, text edit, caption edit, pin and delete: each is prepared, approved by the caller, declined by one rendered dialog of its family and verb (`send`, `change` or `delete`); no dispatch at all |
| `bundle.install` | bundle | 8 | first install of an older synthetic release: `activation_required`, pointer, v2 receipt, Codex and Claude Desktop entries with neighbours kept, skills, no broker start, the session untouched |
| `bundle.upgrade` | bundle | 9 | upgrade to this package's release: `upgraded`/`restarted`, pointer, broker definition, fake launchd bootout then bootstrap, both client entries, the pin moved, the session untouched, the old release kept, `previous` recorded |
| `bundle.rollback` | bundle | 7 | back to the older release: `rolled_back`/`restarted`, pointer, definition, entries, skills, pin, session, the newer release kept |
| `bundle.uninstall` | bundle | 8 | `uninstalled` with everything owned removed (root, plist and job, Codex and Desktop entries, skills), neighbours and the whole session kept; a repeat answers `absent` |

Surface (`SURFACE_COUNTS`): 157 structured results validated, from all 64 tools.

### 5.1 Every tool

`EVERY_TOOL` fixes the order and the expected status of each probe; later probes name what earlier ones
returned (a draft, a handle, a result reference, an upload). Sends and applies use `approved=false`, so no
dialog opens. The statuses are those of the synthetic account: `discover_targets`, `read_history`,
`search_messages` and `search_chats` answer `partial` (the account is too small to prove the provider's end),
`get_attachment` of a text message, `analyze_media` of a PNG, `list_topics`/`read_topic_history` of a private
chat and `read_spreadsheet`/`read_presentation` of a CSV answer `unsupported`; every other tool reaches its
success status (`prepared`, `declined`, `pending`, `revised`, `retained`, `complete`, …).

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

## 8. Receipt v3 and harness contract

```bash
.venv/bin/python packaging/release_consumer_matrix.py inspector-fetch --node <node> --cache <dir>
.venv/bin/python packaging/release_consumer_matrix.py run --commit <sha> --receipt <absolute path> \
    [--wheelhouse <dir>] [--toolhouse <dir>] [--node <node> --inspector-cache <dir>] [--workdir <dir>] [--real-dialog]
.venv/bin/python packaging/release_consumer_matrix.py check --receipt <path> [--commit <sha>] [--wheelhouse <dir>]
```

- v3 adds `installer` (distribution, version, sha256 as the commit's gate pins it, toolhouse provided or
  downloaded) and `inspector` (the pass or unavailable form), adds `inspector` and `inspector_cells` to the
  summary and drops `inspector_cli.synthetic` from `owner_cells`. A v2 receipt is refused.
- **The schema is frozen.** `MATRIX_SCHEMA_FINGERPRINT` is the fingerprint these cells were written for (the
  0.58 surface). `run` refuses another census before any session (`schema_mismatch`), `check_receipt` refuses a
  receipt with another fingerprint on its own, and `check` refuses a commit whose census differs. A deliberate
  schema change must update the cells, their counts, the tool tables and the constant together; a unit test
  holds the constant and every table to the source.
- Every v2 rule still holds (commit, wheel, census, closure, listing, harness and gate bindings; frozen
  counts; fixed refusal codes). New fixed codes: `toolhouse_invalid`, `installer_failed`, `inspector_invalid`,
  `cache_invalid`, `node_invalid`, `fetch_failed`.

## 9. Decisions and rejected options

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

## 10. Findings recorded for the audit (not changed here)

- **Retained selections of messages with entities.** `selection_projection.message_projection` accepts an
  entity only with exactly the keys `offset`, `length`, `type`, while TDLib's JSON writes every object with its
  `@type` (`"@type": "textEntity"`), which the forward projection's own check requires. On the synthetic
  account the hashtag message 55 forwards but `retain_selection` of it answers `selection_unavailable`; the
  selection tests use entities without `@type`. Not yet checked on the real account. The matrix retains a
  message without entities (56) and does not freeze this behaviour.
- **`prepare_text_send` without `formatted_text_send`.** A styled text send under a policy without the
  capability answers `status: error` with `topic draft could not be prepared safely`, where the reply tools
  answer `capability_disabled` with `missing_capabilities`.

## 11. Seams for part 2 (agent cells)

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
