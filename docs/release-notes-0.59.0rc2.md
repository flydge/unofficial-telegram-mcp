# 0.59.0rc2 — final acceptance candidate, 2026-10-26

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate replaces [0.59.0rc1](release-notes-0.59.0rc1.md) with one fix found while installing that candidate on
the owner's Mac ([task record](plans/final-acceptance-spec.tasks.md), [changelog](../CHANGELOG.md)). It is a release
candidate with release-gate evidence; the owner session and Gpub follow.

## Fixed

- **An enrollment survives a reboot.** `enrollment.json` bound the session root and `tdlib/` by device number and
  inode. macOS assigns a volume's device number when it mounts it, so after a reboot or a remount every directory
  reports another device number with the same inode, and the enrollment read as `enrollment_inconsistent`: the
  broker's preflight refused, and a broker started after the change could not resolve its credentials. The
  directories are now bound by inode on one volume; the recorded device numbers stay in the record and are not
  compared, and a replaced directory (another inode) is still refused. Every release since 0.52 had this defect; a
  broker started before the change kept running.
- **Throttled Codex runs in the consumer matrix.** Codex waits out an HTTP 429 from its provider without any event;
  the matrix now reads Codex's retry notice and retries such an attempt instead of failing the cell (no change to
  the product).

Package version 0.59.0rc2, public contract version 1, 64 tools; tool schemas are those of 0.59.0rc1.

## Evidence

- The full isolated-HOME suite and the release gate on the tagged commit, and the consumer-matrix receipt with
  the synthetic, Inspector, Codex and Claude Code columns on the same commit.
- New tests: a record written before a remount reads; a record whose root or session inode differs, and a replaced
  session directory, still refuse. The owner's real enrollment record, which 0.59.0rc1 and 0.52.0 both refused after
  the reboot, reads under the fix (checked read-only).

## Known limits

Everything listed under Known limits in the [0.59.0rc1 notes](release-notes-0.59.0rc1.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
