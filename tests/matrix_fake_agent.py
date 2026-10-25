"""A stand-in for `codex` and `claude` in the consumer matrix's agent-column tests (never a model).

The test writes a wrapper named like the CLI that runs this file with `--as codex|claude`, a behaviour and the
harness path, then the CLI's own arguments. It answers `--version` like the CLI, finds the one MCP server the harness
configured (Codex: config.toml in CODEX_HOME; Claude Code: the --mcp-config file), recognises the cell by its exact
request, drives the proxy with the official SDK the way an honest agent would, and prints the CLI's event stream.
Its first event reports what it could see of its isolation (HOME, the configuration home, the working directory, the
configured servers and the environment's names), which the tests read from the kept evidence.

Behaviours: honest; workaround (after the refusal, prepares a plain text send instead); shell (Codex reports a shell
command); not_logged_in (the CLI's login failure, no tool call); logout_after_census (agent.census as shell, every
later cell as not_logged_in: a login lost mid-column); transient (the first attempt of each cell fails at capacity
before any tool call); prose (no tool call; the model's own words name login and capacity problems, which are
no provider failure); refresh (Codex rewrites auth.json as a token refresh does); hang (Codex refreshes auth.json,
reports its pid and its broker's HOME to --report, then sleeps far past the test, like an agent that would outlive
an interrupted harness); touch_config and new_project (Claude Code changes the caller's ~/.claude.json, or adds a
project folder, as a run that loaded or wrote the user's configuration would); bad_version.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import tomllib


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def refresh(auth: Path) -> None:
    """What a token refresh leaves in auth.json: the same keys, new token values."""
    value = json.loads(auth.read_text())
    value["tokens"] = {key: f"{token}-refreshed" for key, token in value["tokens"].items()}
    auth.write_text(json.dumps(value) + "\n")


def unwrap(value):
    return value["result"] if isinstance(value, dict) and set(value) == {"result"} else (value or {})


async def act(matrix, cell: str, call, behaviour: str) -> str:
    """The honest agent's calls for one cell, through `call(tool, arguments)`; its answer."""
    chat, family = matrix.FIRST_CHAT, matrix.FAMILY_CHAT
    reply_to = {"chat_id": family, "message_id": matrix.FAMILY_MESSAGE}
    if cell == "agent.census":
        manifest = await call("_manifest", {"check_broker": True})
        return (f"The server is {manifest['compatibility']['status']}: {len(manifest['available_tools'])} tools are "
                f"available; schema fingerprint {manifest['schema_fingerprint']}.")
    if cell in ("agent.read", "agent.alias_server"):
        await call("read_history", {"target": chat, "mode": "latest"})
        return f"The newest message is a document captioned “{matrix.OWN_CAPTION}”."
    if cell == "agent.search_cursor":
        cursor, pages = None, 0
        while pages < 8:
            page = await call("search_messages", {"target": chat, "query": "Matrix synthetic", "mode": "latest",
                                                  "limit": 1, **({"cursor": cursor} if cursor else {})})
            pages, cursor = pages + 1, page.get("next_cursor")
            if not cursor:
                break
        return "Matches: " + "; ".join((matrix.OWN_CAPTION, matrix.OWN_TEXT, matrix.SOURCE_TEXT)) + "."
    if cell == "agent.refusal":
        refused = await call("prepare_reply_text_send", {"recipient": family, "text": matrix.REFUSAL_REPLY_TEXT,
                                                         "reply_to": reply_to})
        if behaviour == "workaround":
            await call("prepare_text_send", {"recipient": family, "text": matrix.REFUSAL_REPLY_TEXT})
        return ("The reply was refused (capability_disabled); the owner has to enable "
                + ", ".join(refused.get("missing_capabilities") or []) + ". Nothing was prepared or sent.")
    if cell == "agent.approval_timeout":
        draft = await call("prepare_text_send", {"recipient": chat, "text": matrix.TIMEOUT_PROBE})
        sent = await call("send_prepared_text", {"draft_id": draft.get("draft_id"), "approved": True})
        return f"The owner's dialog ended with {sent.get('status')}: it timed out, so nothing was sent."
    if cell == "agent.album_prepare":
        items = []
        for name in ("matrix-a.txt", "matrix-b.txt"):
            created = await call("create_local_artifact", {"file_name": name, "content": f"Matrix {name}\n"})
            items.append({"artifact_id": created.get("artifact_id")})
        await call("prepare_album_send", {"recipient": chat, "items": items, "group": "documents"})
        return "The documents album is prepared as a draft and was not sent."
    if cell == "agent.own_message_prepare":
        await call("prepare_text_edit", {"chat": chat, "message_id": matrix.OWN_TEXT_MESSAGE,
                                         "new_text": matrix.EDIT_TEXT})
        return "The edit is prepared as a draft and was not applied."
    await call("prepare_reply_text_send", {"recipient": family, "text": matrix.FAMILY_REPLY_TEXT,
                                           "reply_to": reply_to})
    return "The reply is prepared as a draft and was not sent."


async def drive(matrix, cell: str, server: dict, behaviour: str, record) -> str:
    from mcp import Client, StdioServerParameters
    params = StdioServerParameters(command=server["command"], args=list(server.get("args") or []),
                                   env=dict(server.get("env") or {}), cwd=server.get("cwd"))
    async with Client(params, read_timeout_seconds=120, mode="legacy") as client:
        await client.list_tools()

        async def call(tool: str, arguments: dict) -> dict:
            result = await client.call_tool(tool, arguments)
            value = unwrap(result.structured_content)
            record(tool, arguments)
            return value if isinstance(value, dict) else {}
        return await act(matrix, cell, call, behaviour)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--as", dest="role", choices=("codex", "claude"), required=True)
    parser.add_argument("--behaviour", default="honest")
    parser.add_argument("--harness", required=True)
    parser.add_argument("--report")
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    rest = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    if rest == ["--version"]:
        if args.behaviour == "bad_version":
            print("version unknown")
        else:
            print("codex-cli 0.162.0" if args.role == "codex" else "2.1.304 (Claude Code)")
        return 0
    spec = importlib.util.spec_from_file_location("matrix_for_fake_agent", args.harness)
    matrix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matrix)
    if args.role == "codex":
        request, answer_path = rest[-1], Path(rest[rest.index("--output-last-message") + 1])
        config_home = Path(os.environ["CODEX_HOME"])
        servers = tomllib.loads((config_home / "config.toml").read_text())["mcp_servers"]
    else:  # The default configuration location (the caller's HOME), for the login only.
        request, answer_path = rest[rest.index("-p") + 1], None
        config_home = Path(os.environ["CLAUDE_CONFIG_DIR"]) if "CLAUDE_CONFIG_DIR" in os.environ else None
        servers = json.loads(Path(rest[rest.index("--mcp-config") + 1]).read_text())["mcpServers"]
    [configured] = servers  # the default's name, or an alias's `<server>_<alias>`
    bases = (configured, configured.removesuffix(f"_{matrix.ALIAS}"))
    cell = next(cell for cell in matrix.AGENT_IDS for base in bases if matrix.agent_request(cell, base) == request)
    auth = config_home / "auth.json" if config_home else Path(os.devnull, "absent")
    emit({"type": "fake.isolation", "home": sorted(os.listdir(os.environ["HOME"])), "home_path": os.environ["HOME"],
          "config": sorted(os.listdir(config_home)) if config_home else None, "cwd": sorted(os.listdir(os.getcwd())),
          "servers": sorted(servers), "environment": sorted(os.environ),
          "auth_sha256": hashlib.sha256(auth.read_bytes()).hexdigest() if auth.exists() else None,
          "argv": rest[:-1] if args.role == "codex" else [part for part in rest if part != request]})
    [(name, server)] = servers.items()
    if args.behaviour == "touch_config" and args.role == "claude":
        with open(Path(os.environ["HOME"]) / ".claude.json", "a") as stream:
            stream.write("\n")
    if args.behaviour == "new_project" and args.role == "claude":
        (Path(os.environ["HOME"]) / ".claude" / "projects" / "-matrix-cell" / "memory").mkdir(parents=True)
    if args.behaviour == "logout_after_census":
        args.behaviour = "shell" if cell == "agent.census" else "not_logged_in"
    if args.behaviour == "not_logged_in":
        if args.role == "codex":
            emit({"type": "turn.started"})
            emit({"type": "error", "message": "unexpected status 401 Unauthorized: Missing bearer authentication"})
            emit({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
        else:
            emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Not logged in"}]},
                  "error": "authentication_failed"})
            emit({"type": "result", "subtype": "success", "is_error": True,
                  "result": "Not logged in · Please run /login"})
        return 1
    if args.behaviour == "prose":
        words = ("The Telegram server is temporarily unavailable, at capacity or rate limited; you are not logged in, "
                 "please sign in or run /login (401 Unauthorized, refresh token expired).")
        if args.role == "codex":
            for event in ({"type": "thread.started", "thread_id": "fake"}, {"type": "turn.started"},
                          {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": words}},
                          {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": words}},
                          {"type": "turn.completed", "usage": {}}):
                emit(event)
            answer_path.write_text(words)
        else:
            emit({"type": "system", "subtype": "init"})
            emit({"type": "assistant", "message": {"content": [{"type": "text", "text": words}]}})
            emit({"type": "result", "subtype": "success", "is_error": False, "result": words})
        return 0
    if args.behaviour == "transient":
        marker = Path(os.environ["HOME"]) / f".attempted-{cell}"
        if not marker.exists():
            marker.write_text("1")
            emit({"type": "error", "message": "We're currently experiencing high demand: model at capacity"})
            emit({"type": "turn.failed", "error": {"message": "at capacity"}})
            return 1
    if args.behaviour in ("refresh", "hang") and args.role == "codex":
        refresh(auth)
    if args.behaviour == "hang":
        report = Path(args.report)
        report.with_suffix(".tmp").write_text(json.dumps({"pid": os.getpid(), "broker_home": server["env"]["HOME"]}))
        report.with_suffix(".tmp").rename(report)
        time.sleep(300)
        return 1
    calls = []
    answer = asyncio.run(drive(matrix, cell, server, args.behaviour, lambda tool, arguments: calls.append(
        (tool, arguments))))
    if args.role == "codex":
        emit({"type": "thread.started", "thread_id": "fake"})
        emit({"type": "turn.started"})
        for index, (tool, arguments) in enumerate(calls):
            emit({"type": "item.completed", "item": {"id": f"item_{index}", "type": "mcp_tool_call", "server": name,
                                                      "tool": tool, "arguments": arguments, "status": "completed"}})
        if args.behaviour == "shell":
            emit({"type": "item.completed", "item": {"id": "item_shell", "type": "command_execution",
                                                      "command": "ls", "status": "completed"}})
        emit({"type": "item.completed", "item": {"id": "item_answer", "type": "agent_message", "text": answer}})
        emit({"type": "turn.completed", "usage": {}})
        answer_path.write_text(answer)
    else:
        emit({"type": "system", "subtype": "init"})
        for index, (tool, arguments) in enumerate(calls):
            emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{index}",
                                                                "name": f"mcp__{name}__{tool}", "input": arguments}]}})
            emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{index}"}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": answer})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
