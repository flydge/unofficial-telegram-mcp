# 0.51.0 — 2026-10-13

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This source release advances the previous public 0.35.0 baseline to the accepted
0.51.0 implementation. Public contributor ancestry is preserved; private development
history, local sessions and private acceptance records are not distribution inputs.
The [original plan](plans/initial-roadmap.md) and [current progress](roadmap.md) make
completed slices and remaining work accessible in Git.

## Available behavior

The 44-tool candidate STDIO contract covers bounded discovery and exact-chat search; selected
message/history/reply/topic reading; selected document and media analysis; local
artifact transfer; draft inspection/revision/cancellation; and explicitly approved
text, document, photo and voice-note sending. Extended capabilities remain opt-in.
Client tool visibility grants no additional broker permission.

Compared with the previous public snapshot, conservative reply-source admission
includes additional inert DateTime, URL, email, phone-number, bank-card and TextUrl
message-text subsets. Exact forum-topic text sends/replies use `topic_text_send`.
Explicit outgoing Bold, Italic, Underline, Strikethrough and Spoiler use
`formatted_text_send`. Unsupported source combinations and formatted captions are
not silently accepted; see the reference for the exact bounds.

Package version is 0.51.0, public contract version 1 and private IPC version 2.
Topic/style inputs changed public schemas. Deploy matching proxy, broker, policy
and optional skill together; a cached fingerprint is not proof of compatibility.
Restart invalidates in-memory drafts, uploads and cursors. An uncertain send is
never automatically repeated.

Release diagnostics inspect environment and evidence pins and provide an installed
SDK preflight. Their `acceptance: false` reports are preparation diagnostics, not a
replacement for real consumer/provider acceptance.

## Corrections included

- Corrupt PNG checksum errors produce an explicit safe reader error, without text
  or image blocks, rather than being presented as an unavailable broker.
- A formatted ordinary self-send can correlate TDLib's Saved Messages topic only
  with the freshly verified account's own ID. The outgoing `topic_id=null` request
  and strict content/entities/account/pending-to-final checks remain unchanged.

## Accepted scenarios

The corrected implementation passed the complete automated suite, noneditable
package installation, source/archive/wheel equality, installed RECORD checks and
independent review. Installed official MCP SDK and genuine persistent Inspector
sessions exercised public schemas, live reads/history/search continuation, Unicode,
image blocks and corrupt-image recovery, draft revision/cancellation, styles, topic
paths, approval denial and schema-mismatch recovery.

Native Codex read a selected actual Telegram TXT attachment through both readers;
the observed bytes and hashes matched. This does not distinguish a cache hit from
a fresh network transfer. A new separately approved corrected Saved Messages send
was provider-confirmed and read back at its exact anchor, also independently through
the official SDK. An earlier forum test send had a provider-confirmed receipt and
exact readback.

The final publication changes add license and roadmap documentation and package
metadata. Product Python modules and tool schemas are unchanged from the corrected
implementation that underwent those live checks. Packaging and automated checks
are repeated for the publication tree.

## Known limits

- Apple Silicon macOS, pinned TDLib, trusted Keychain provisioning and an already
  authorized dedicated session remain prerequisites. A first-time login wizard and
  reproducible TDLib provisioning are not included.
- Current consumer evidence is for bounded tested scenarios. Every source family,
  protocol version, first-time-user setup and unattended operation is not established.
- Visual rendering of every outgoing style was not independently checked.
- A historical unknown send remains unknown. A later reply source was deleted;
  its safe refusal was accepted, with no new reply-delivery claim for that source.
- Forward/copy, native video/albums, multiple accounts and message-management actions
  remain on the [roadmap](roadmap.md). The complete original plan is not finished.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**. The current PyMuPDF dependency
is the practical reason for this choice. A possible future replacement and license
change are explained in [licensing](licensing.md); upstream copyrights and notices
are retained in [third-party notices](third-party-notices.md).

The public name remains **Unofficial Telegram MCP**. Compatibility identifiers stay
`telegram-search-mcp`, `telegram_search`, the Python namespace, CLI names and
`TelegramSearchMCP` runtime paths. Dependencies are not upgraded in this release.

The candidate adds default-off exact retained selections for chosen messages and
owned files. Technical selection tests do not close installed/native/live gates.
