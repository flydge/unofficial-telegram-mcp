![Unofficial Telegram MCP](docs/assets/unofficial-telegram-mcp-header.png)

# Unofficial Telegram MCP

*by Flydge*

Connect Telegram to **Claude Code, Codex, or another local MCP client**.

**Published version 0.51.0:** 43 tools, opt-in exact forum-topic text sends/replies, and five explicit outgoing text styles. The accepted bounded scope has installed SDK, Inspector and native Codex evidence; first-time Telegram provisioning and the broader roadmap remain unfinished. See [release notes](docs/release-notes-0.51.0.md), the [tool reference](docs/reference.md), and [development progress](docs/roadmap.md).

**0.52 release candidate** (this checkout, `0.52.0.dev0`, not yet tagged):

- **QR-first login.** Install, then `connect`: a local page on your Mac shows a QR code, you scan
  it in Telegram on your phone, and you type a 2FA password, if you use one, only on that page. No API
  ID or hash, phone number or login code passes through your assistant. This was verified for real on
  the owner's Mac (login, QR refresh, cancel, expiry, 2FA, revoke and reconnect, and a search from
  Claude Code and from Codex). Installing the bundle on a clean macOS user is deferred to the
  backlog. See [candidate installation](#unreleased-local-candidate).
- **55 tools.** Four new opt-in actions are each behind their own default-off capability, together
  with `send`. Each has a complete preview, a local owner approval and one non-retried attempt, and
  each was verified with real approved runs on 20.10.2026:
  - forward or copy one message ([`forward_send`](docs/reference.md#exact-forwardcopy-f14));
  - send one MP4/MOV as a native video ([`video_send`](docs/reference.md#native-video-send-f15a));
  - move one chat between Main and Archive, and replace the text of one of your own messages
    ([`archive_chats`, `edit_own_text`](docs/reference.md#archiveunarchive-and-own-text-edit-f17a)).
- **Clearer refusals.** A tool whose capability is off now says so, for example
  `capability_disabled: read_history`, instead of a bare error.
- **Also in the candidate:** default-off [retained selections](docs/reference.md#exact-retained-selections-opt-in-candidate)
  for reusing an exact chosen message or file, and local-upload authority tools. These have
  installed-SDK evidence with synthetic data, but no live acceptance.

**Free and open source under [AGPLv3](LICENSE).** This license was chosen because the current PDF reader uses PyMuPDF. A future PDF replacement and different license are being considered; read the [licensing rationale](docs/licensing.md). The [original development plan](docs/plans/initial-roadmap.md) and [current roadmap](docs/roadmap.md) are tracked in Git.

**Before connecting:** complete the [one-time local setup](docs/install.md) on an Apple Silicon Mac, including Telegram authorization. Your assistant and Unofficial Telegram MCP must run on the same Mac. Replace `/ABSOLUTE/PATH/TO/unofficial-telegram-mcp` below with your installation folder.

## Claude Code

*The 0.52 candidate installer registers this for you; see [the candidate below](#unreleased-local-candidate). The manual command below is for the published 0.51 install.*

Run in your terminal:

```bash
claude mcp add --transport stdio --scope user telegram_search -- \
  "/ABSOLUTE/PATH/TO/unofficial-telegram-mcp/.venv/bin/telegram-search-mcp"
```

Open Claude Code and run `/mcp` to check the connection. [Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

## Codex

*The 0.52 candidate installer registers this for you as well.*

Add this block to `~/.codex/config.toml`, preserving your existing settings:

```toml
[mcp_servers.telegram_search]
command = "/ABSOLUTE/PATH/TO/unofficial-telegram-mcp/.venv/bin/telegram-search-mcp"
startup_timeout_sec = 30
tool_timeout_sec = 600
required = true
```

Restart Codex and open a fresh chat. [Codex MCP guide](https://developers.openai.com/codex/mcp).

## Other MCP clients

Add a **local STDIO server** named `telegram_search`. Use these fields in your client's MCP configuration:

```json
{
  "command": "/ABSOLUTE/PATH/TO/unofficial-telegram-mcp/.venv/bin/telegram-search-mcp",
  "args": []
}
```

Set the tool timeout to **600 seconds** if available, then reconnect the client.

## Check the connection

Ask your assistant:

> Use the `_manifest` tool from Unofficial Telegram MCP with `check_broker=true` and report whether compatibility is `compatible`.

[Setup and troubleshooting](docs/install.md)

# Unreleased local candidate

This checkout is release candidate `0.52.0.dev0`. Its private authenticated bundle supplies
standalone Python, the pinned dependencies and TDLib, and a recoverable first installation on
Apple Silicon macOS 26+, with no Homebrew, Git or compiler needed. See
[candidate installation](docs/install.md).

**QR-first login (0.52):**
- **What the installer does.** It registers the MCP server in Codex and, when the `claude` CLI is
  found at a fixed location, in Claude Code. Otherwise it returns the exact command to run yourself.
  It copies the paired skills to `~/.agents/skills` and, if Claude Code is present, `~/.claude/skills`.
- **How you connect.** Your assistant runs `connect`: a local browser page shows a QR code, you scan
  it in Telegram on your phone (Settings → Devices → Link Desktop Device) and enter 2FA only on that
  page. A bundle assembled with the application identity needs no API ID or hash at all. The
  Terminal phone/code setup remains as a fallback.
- **Status.** It was accepted for real on the owner's Mac on 20.10.2026. Installing the bundle on a
  clean macOS user is deferred to the backlog.
- **Open beyond 0.52.0:** public hosting, Developer ID signing and notarization, and complete
  source/redistribution review.

Details and the shared-API-identity risk:
[candidate installation](docs/install.md#private-bundled-candidate-0520dev0).
