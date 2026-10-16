![Unofficial Telegram MCP](docs/assets/unofficial-telegram-mcp-header.png)

# Unofficial Telegram MCP

*by Flydge*

Connect Telegram to **Claude Code, Codex, or another local MCP client**.

**Version 0.51.0:** 43 tools, opt-in exact forum-topic text sends/replies, and five explicit outgoing text styles. The accepted bounded scope has installed SDK, Inspector and native Codex evidence; first-time Telegram provisioning and the broader roadmap remain unfinished. See [release notes](docs/release-notes-0.51.0.md), the [tool reference](docs/reference.md), and [development progress](docs/roadmap.md).

This checkout's build candidate adds a 44th tool, default-off `retain_selection`,
for exact chosen message/file reuse. Its typed source alternatives and refusal
contract change the schema fingerprint. This new slice still requires independent
review and installed/native/live acceptance; earlier accepted evidence does not
cover it. See the [retained selection reference](docs/reference.md#exact-retained-selections-opt-in-candidate).

The candidate surface is now 51 tools. Two of them, `prepare_forward_send` and
`send_prepared_forward`, forward or copy exactly one existing message between two exact
chats behind the default-off `forward_send` capability (together with `send`), with a
complete preview, owner approval and one non-retried attempt. Real approved delivery is
still pending; see the [forward/copy reference](docs/reference.md#exact-forwardcopy-f14).
Two more, `prepare_video_send` and `send_prepared_video`, send one owned MP4/MOV artifact
(at most 64 MiB) as a native Telegram video behind the default-off `video_send` capability
(together with `send`): duration, dimensions and codec are read locally from the file's own
boxes and shown as declared by the file, and the send follows the same preview, owner
approval and single non-retried attempt. A real approved video send is still pending; see the
[native video reference](docs/reference.md#native-video-send-f15a).


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

This checkout is development candidate `0.52.0.dev0`. Its private authenticated
bundle supplies Python/native dependencies and recoverable first installation
on Apple Silicon macOS 26+. See [candidate installation](docs/install.md).
Published 0.51 evidence remains separate; native onboarding, public hosting,
publisher trust and complete source/redistribution acceptance remain open.

**QR-first login (0.52):** the candidate installer registers the MCP server in **both
Claude Code and Codex** and installs the paired skills for both. Your assistant then runs
`connect`: a local browser page on your Mac shows a QR code, you scan it in Telegram on
your phone (Settings → Devices → Link Desktop Device) and enter 2FA only on that page.
No API ID or hash, phone number or login code passes through the assistant, and a bundle
assembled with the application identity needs no API ID/hash entry at all. This is technically implemented and tested with
fakes; real acceptance on a clean macOS user is pending, so it is not yet a released or
accepted feature. The Terminal phone/code setup remains as a fallback. Details and the
shared-API-identity risk: [candidate installation](docs/install.md#private-bundled-candidate-0520dev0).
