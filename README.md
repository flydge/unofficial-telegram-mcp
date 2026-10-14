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


**Free and open source under [AGPLv3](LICENSE).** This license was chosen because the current PDF reader uses PyMuPDF. A future PDF replacement and different license are being considered; read the [licensing rationale](docs/licensing.md). The [original development plan](docs/plans/initial-roadmap.md) and [current roadmap](docs/roadmap.md) are tracked in Git.

**Before connecting:** complete the [one-time local setup](docs/install.md) on an Apple Silicon Mac, including Telegram authorization. Your assistant and Unofficial Telegram MCP must run on the same Mac. Replace `/ABSOLUTE/PATH/TO/unofficial-telegram-mcp` below with your installation folder.

## Claude Code

Run in your terminal:

```bash
claude mcp add --transport stdio --scope user telegram_search -- \
  "/ABSOLUTE/PATH/TO/unofficial-telegram-mcp/.venv/bin/telegram-search-mcp"
```

Open Claude Code and run `/mcp` to check the connection. [Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

## Codex

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
