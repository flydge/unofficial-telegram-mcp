![Unofficial Telegram MCP](docs/assets/unofficial-telegram-mcp-header.png)

# Unofficial Telegram MCP

*by Flydge*

Connect Telegram to **Claude Code, Codex, or another local MCP client**.

**Version 0.52.0** ([release notes](docs/release-notes-0.52.0.md)):

- **QR-first login.** Install, then `connect`: a local page on your Mac shows a QR code, you scan
  it in Telegram on your phone, and you type a 2FA password, if you use one, only on that page. No API
  ID or hash, phone number or login code passes through your assistant. This was verified for real on
  the owner's Mac (login, QR refresh, cancel, expiry, 2FA, revoke and reconnect, and a search from
  Claude Code and from Codex). A source install first stores your own API pair with `api-credentials`;
  only the private bundle carries an application identity. Installing that bundle on a clean macOS
  user is deferred to the backlog. See [the private bundle](#private-bundle-not-distributed).
- **55 tools.** Four new opt-in actions are each behind their own default-off capability, together
  with `send` (archive and edit also need `read`). Each has a complete preview, a local owner approval and one non-retried attempt, and
  each was verified with real approved runs on 20.10.2026:
  - forward or copy one message ([`forward_send`](docs/reference.md#exact-forwardcopy-f14));
  - send one MP4/MOV as a native video ([`video_send`](docs/reference.md#native-video-send-f15a));
  - move one chat between Main and Archive, and replace the text of one of your own messages
    ([`archive_chats`, `edit_own_text`](docs/reference.md#archiveunarchive-and-own-text-edit-f17a)).
- **Clearer refusals.** A tool whose capability is off now says so, for example
  `capability_disabled: read_history`, instead of a bare error.
- **Also in 0.52.0:** default-off [retained selections](docs/reference.md#exact-retained-selections-opt-in-candidate)
  for reusing an exact chosen message or file, and local-upload authority tools. These have
  installed-SDK evidence with synthetic data, but no live acceptance.

**Previous version 0.51.0:** 43 tools, opt-in exact forum-topic text sends/replies, and five explicit outgoing text styles. The accepted bounded scope has installed SDK, Inspector and native Codex evidence; first-time Telegram provisioning and the broader roadmap remain unfinished. See [release notes](docs/release-notes-0.51.0.md), the [tool reference](docs/reference.md), and [development progress](docs/roadmap.md).

**Free and open source under [AGPLv3](LICENSE).** This license was chosen because the current PDF reader uses PyMuPDF. A future PDF replacement and different license are being considered; read the [licensing rationale](docs/licensing.md). The [original development plan](docs/plans/initial-roadmap.md) and [current roadmap](docs/roadmap.md) are tracked in Git.

**Before connecting:** complete the [one-time local setup](docs/install.md) on an Apple Silicon Mac, including Telegram authorization. Your assistant and Unofficial Telegram MCP must run on the same Mac. Replace `/ABSOLUTE/PATH/TO/unofficial-telegram-mcp` below with your installation folder.

## Claude Code

*The 0.52 private-bundle installer registers this for you; see [the private bundle](#private-bundle-not-distributed). The manual command below is for a source install.*

Run in your terminal:

```bash
claude mcp add --transport stdio --scope user telegram_search -- \
  "/ABSOLUTE/PATH/TO/unofficial-telegram-mcp/.venv/bin/telegram-search-mcp"
```

Open Claude Code and run `/mcp` to check the connection. [Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

## Codex

*The 0.52 private-bundle installer registers this for you as well.*

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

# Private bundle (not distributed)

Version 0.52.0 also has a private authenticated bundle (not distributed); its current development
candidate is 0.55.0.dev0. It supplies standalone Python, the pinned dependencies and TDLib, and a
recoverable first installation on Apple Silicon macOS 26+, with no Homebrew, Git or compiler needed.
See [installation](docs/install.md).

**Publisher Team ID: `256X5MUA6S`.** Signed release assets (0.53 and later; none is published yet)
carry this Developer ID Team ID. Before running anything you download, compare it with the `team_id`
of `plugins/telegram-local/release-pins.json` at the release tag and with the DMG's signature, as the
[install guide](docs/install.md#publisher-team-id-and-what-binds-what) shows.

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
- **Open beyond 0.52.0:** public hosting, the Developer ID signed and notarized build, and complete
  source/redistribution review.

Details of the 0.55.0.dev0 candidate and the shared-API-identity risk:
[the private bundle](docs/install.md#private-bundle-0550dev0).
