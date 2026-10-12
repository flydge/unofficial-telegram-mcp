# 0.51.0 publication candidate — 2026-10-12

[← Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Security](security.md)

This is a local publication candidate, not a published release or a claim of live readiness. Public `main` was version 0.35.0 at `796f95d22ae7016ae8c7a313e96e15233aba7531` when the candidate was prepared. The implementation snapshot is version 0.51.0; the public contributor history is preserved without importing the private development history.

## Current scope

The existing 43-tool STDIO contract covers bounded Telegram discovery, exact-chat search, selected message/history/reply/topic reads, selected document/media analysis, local artifact transfer, draft inspection, and explicitly approved text/document/photo/voice sending. Extended readers and reply-source families require their existing opt-in capabilities; client tool visibility grants no broker permission.

Since the public 0.35.0 snapshot, the existing reply-source workflows admit additional conservative inert DateTime, URL, email, phone-number, bank-card and TextUrl message-text evidence subsets. Exact forum-topic text sends and replies require `topic_text_send`; explicit outgoing Bold, Italic, Underline, Strikethrough and Spoiler styles require `formatted_text_send`. These remain default-off. Every outgoing attempt still requires review of its complete frozen preview and fresh applicable approval. See the reference and security model for precise bounds, source-capability conjunctions, unsupported entities, revocation, and terminal status semantics.

Version 0.51.0 retains contract version 1, private IPC version 2 and 43 tools. Topic and style inputs extend schemas, so a previously cached fingerprint cannot be reused as proof of compatibility. Deploy matching proxy, broker, policy and skill together only through a reviewed activation/rollback procedure. Restart clears in-memory drafts, uploads and cursors; uncertain sends must never be replayed.

Release preparation diagnostics now include environment pin checks, evidence/preflight inspection, and an installed SDK probe. Their reports use `acceptance: false`; they do not run or replace consumer/provider acceptance. See [release-preflight](release-preflight.md).

## Evidence and remaining gates

Historical unit, synthetic SDK and Inspector checks establish bounded technical properties only. This document does not claim native Codex, live Telegram delivery, persistent Inspector lifecycle, clean-user installation or unattended operation were accepted for this exact candidate. The end-to-end owner must bind fresh independent review, package/build/install checks and each applicable real consumer/provider scenario to the final public commit and package digests. Missing access or failed scenarios remain open gates.

The existing installation gap remains: Apple Silicon macOS, pinned TDLib, trusted Keychain provisioning and an already authorized dedicated TDLib session are required. There is no first-time login wizard or reproducible TDLib provisioning script. A clone of today's public `main` installs 0.35.0, not this unpublished candidate.

Project-license selection, dependency notices/provenance review and explicit publication approval are separate owner decisions. No project license is introduced by this candidate. Consult the [dependency reference inventory](third-party-notices.md); it is not a complete legal clearance or a substitute for distribution notices.

## Public presentation

The public name, banner, install guide and repository links remain **Unofficial Telegram MCP**. Compatibility identifiers remain `telegram-search-mcp`, `telegram_search`, the Python namespace, CLI names and `TelegramSearchMCP` runtime paths. The changes made while exporting the 0.51.0 implementation are branding strings, public documentation, package documentation inclusion and matching test expectations; no new behavior or dependency upgrade is introduced by this preparation step.
