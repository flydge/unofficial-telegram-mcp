# 0.54.0rc1 — release candidate, 2026-10-23

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate adds conversational usability to 0.53.0rc2
([spec](plans/0.54-conversational-usability-spec.md), [task record](plans/0.54-conversational-usability-spec.tasks.md),
[changelog](../CHANGELOG.md)). It is a local annotated tag with release-gate evidence; nothing is published.
The owner rows (real approval dialogs, installed file coverage, conversational tasks on the owner's
account, the profile switch on an installed bundle) wait for the owner session.

## Available behavior

- **Short tool contracts.** Every tool description states what it does, the capabilities it needs, where key
  inputs come from, the approval or retry rule and that Telegram data is untrusted (28,430 characters in
  total, down from 123,138). Every tool has an MCP `title`, annotations are normalized, key inputs have
  field descriptions, and four prompts (`find_and_summarize`, `reply_with_file`, `unread_overview`,
  `check_connection`) follow the loaded policy and grant nothing.
- **Refusals that say what to do.** Argument refusals name only the tool's own missing or invalid parameters
  and count unexpected ones, never echoing a value. Reply sources that need a disabled capability return
  `capability_disabled` with `missing_capabilities`. Closed `next_action` hints (`check_connection`,
  `connect_account`, `update_installation`, `enable_capability`, `search_again`, `restart_listing`,
  `check_send_status`) appear on the responses users hit most. A `capability_disabled` guard refusal names
  the profile that unlocks it.
- **Approvals.** One renderer for every kind (summary, exact payload behind a gutter, technical block) and a
  native alert with a 120-second timer; `not_approved` is split into `declined`, `timeout` and
  `dialog_unavailable`, none of which sends. Only one owner dialog is open at a time: another send meanwhile
  answers `dialog_unavailable` and stays pending.
- **Evidence and context.** The newer readers return validated `t.me` message links; the retained-selection
  lifetime is the policy setting `selection_ttl_seconds` (default 900, at most 1800).
- **Capability profiles.** The owner switches `readers`, `send`, `replies`, `forward`, `video` and
  `archive_edit` in Terminal with `launcher.py capabilities show | enable | disable <profile>`, after
  quitting the MCP clients; a later rollback keeps working.
- **Lean skill.** The search skill is 259 lines and about 19 KB (down from about 85 KB), checked against the
  built server by a coverage test.

Package version 0.54.0rc1, public contract version 1. **Tool schemas changed** (each change is listed in
`tests/fixtures/schema_changes/`): clients must refresh `tools/list`, and a `runtime.toml` that pins
`expected_schema_fingerprint` is `config_stale` until re-pinned. Deploy matching proxy, broker, policy and
skills together.

## Evidence

- The full isolated-HOME suite and the release gate on the tagged commit, and the consumer-matrix receipt
  (every synthetic cell through the official SDK against the installed wheel) on the same commit.
- An independent whole-version critic (five dimensions, each serious finding checked by two verifiers) and
  re-reviews of every fix round, recorded in the task record.
- The native dialog's timeout path proven from a LaunchAgent; real `codex exec` runs of the conversation
  scenarios on synthetic data, scored on the server's transcript of actions.

## Known limits

- Owner-session rows: new dialogs on real drafts and real clicks; installed file and page coverage on real
  artifacts; two conversational tasks with and without the skill on the owner's account; the profile switch
  on an installed bundle.
- The conversation rubrics decide on actions; checks on the wording of an agent's final answer are advisory
  (free text cannot be checked reliably).
- Open lows recorded in the task record: the answer-wording heuristics use word lists; the matrix does not
  test the plural-profile and `runtime.toml` refusal texts or the lower input bounds; the runner watches only
  shell tools.
- Everything listed under Known limits in the [0.53.0rc2 notes](release-notes-0.53.0rc2.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
