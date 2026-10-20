# Overnight development summary — 16.10.2026

Sanitized summary of one unattended development night. The working log (per-commit table, reviewer
notes and local operational details) is an internal record and is kept outside the repository; the
specs and `.tasks.md` files in this directory carry the durable decisions and evidence.

## Outcome

- **0.52 QR-first onboarding, phases A–E** implemented and reviewed: transport and credential
  resolution, the connect flow, the loopback QR screen, the `connect`/`wait`/`cancel` CLI with a
  transient launchd helper and guarded reconnect, and the installer, skills and docs. Phase F (real
  acceptance) was deferred to the owner; see [the 0.52 tasks](0.52-qr-onboarding-spec.tasks.md).
- **Three opt-in features** implemented, reviewed (twice where they send) and documented, each behind
  its own default-off capability: F14 forward/copy of one message
  ([spec](0.53-forward-copy-spec.md)), F15a native video send ([spec](0.53-native-video-spec.md)) and
  F17a archive/unarchive plus edit of an own text message ([spec](0.54-archive-and-edit-spec.md)).
- Tool count 47 → 55; automated tests 2053 → 2614 passing, 3 skipped. No existing tool schema changed.
  Nothing was pushed.

## Method

Every phase ran as implementer, then the lead re-ran the full isolated-HOME suite, then an independent
critic reviewed it, then fixes and a commit. A final whole-branch review found nothing above low.

## Defects found by the reviews and fixed

- Reconnect: after a phone-side session revoke the broker reported `error` instead of `blocked`, so
  reconnect could not trigger; a failed reconnect could leave the broker stopped; `wait` could loop on a
  stale status; two `connect` runs could race.
- Installer: a Node-installed `claude` could be misread as "no registration entry" and half-publish the
  install; client config roots created 0755 by the clients were refused by an over-strict rule.
- Integration: the launcher hid installer refusal codes; a slow-starting broker was shown as busy; a
  `flood_wait` lost its retry time; a sandboxed agent shell received the wrong fallback; bundle assembly
  did not prove that the payload carried this code.
- Forward/copy: hidden message parts (link targets, URL buttons, unseen link previews) would have been
  sent without being shown and are now refused; already-forwarded sources, look-alike and bidirectional
  characters and missing supersession were fixed.
- Video and archive/edit: an attempted send was reported `expired`; edits now refuse characters the
  provider rewrites; a server error on a state change is `outcome_unknown`.

## Left for the owner at the time

- Real QR acceptance (phase F), the bundle build with the application identity, and one real approved
  run of each new feature. Later real runs are recorded in the `.tasks.md` files.
- Review of the delegated decisions in the three feature specs.
- Two scope questions: text sends accepting characters the provider silently rewrites (closed later; see
  the [changelog](../../CHANGELOG.md)) and the status wording for a send whose authority was lost before
  a later call (left open at the time).
