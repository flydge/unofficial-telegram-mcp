"""Conversation harness through the official SDK (0.54 item E, criterion 10): deterministic, no model.

Each scenario runs a scripted agent through the official MCP SDK client against the demo server
(the real server over STDIO, a synthetic account, scripted approvals) and scores the server's own
transcript with the rubric that `conversation_agent_run.py` applies to real agents. Every argument
the scripted agent passes is read out of an earlier tool result. Mutated copies of the same
transcripts prove that each rubric fails the behaviour it forbids.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters

import conversation_account as account
import conversation_rubrics as rubrics
from conversation_rubrics import SCENARIOS, Run, score

ROOT = Path(__file__).resolve().parents[1]
SERVER = Path(__file__).resolve().with_name("conversation_demo_server.py")
FAKE_ARTIFACT = "artifact_" + "0" * 32 + "_" + "0" * 64 + "_1"


def server_parameters(arguments: list[str], cwd: str) -> StdioServerParameters:
    environment = {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("TMPDIR"):
        environment["TMPDIR"] = os.environ["TMPDIR"]
    return StdioServerParameters(command=sys.executable, args=[str(SERVER), *arguments], env=environment, cwd=cwd)


class Agent:
    """Calls one tool through the SDK; a tool error fails the scripted run."""

    def __init__(self, client):
        self.client = client

    async def __call__(self, tool: str, **arguments) -> dict:
        result = await self.client.call_tool(tool, arguments)
        if result.is_error:
            raise AssertionError(f"{tool}: {result.content[0].text}")
        return rubrics.unwrap(result.structured_content)


async def start(call):
    manifest = await call("_manifest", check_broker=True)
    assert manifest["compatibility"]["status"] == "compatible", manifest["compatibility"]


async def discover_alice(call) -> int:
    """Discovery paged to complete coverage; the one private chat that fits is chosen."""
    hypotheses, seen = ["Alice", "Alice Example"], {}
    page = await call("discover_targets", hypotheses=hypotheses)
    for _ in range(5):
        seen.update({candidate["chat_id"]: candidate for candidate in page["candidates"]})
        if page["status"] == "complete":
            break
        page = await call("discover_targets", hypotheses=hypotheses, cursor=page["next_cursor"])
    assert page["status"] == "complete", page["status"]
    fits = [c for c in seen.values() if c["chat_type"] == "private" and "alice" in c["title"].casefold()]
    assert len(fits) == 1, fits
    return fits[0]["chat_id"]


def first_document(found: dict) -> dict:
    return next(row for row in found["results"] if row["message"]["content_kind"] == "document")


async def find_and_summarize(call) -> str:
    await start(call)
    alice = await discover_alice(call)
    hit = first_document(await call("search_messages", target=alice, query="quarterly report", mode="latest"))
    attachment = await call("get_attachment", anchor=hit["anchor"])
    page = await call("read_attachment_page", artifact_id=attachment["artifact_id"])
    lines = [line for line in page["text"].splitlines() if line.startswith(("Revenue", "Active users", "Risks"))]
    return f"{attachment['file_name']} (from Alice): " + " ".join(lines)


async def reply_with_file(call) -> str:
    await start(call)
    saved = (await call("resolve_target", target="Saved Messages"))["resolved_target"]["chat_id"]
    hit = first_document(await call("search_messages", target=saved, query="budget spreadsheet", mode="latest"))
    attachment = await call("get_attachment", anchor=hit["anchor"])
    alice = await discover_alice(call)
    asked = await call("search_messages", target=alice, query="budget", mode="latest", direction="incoming")
    prepared = await call("prepare_reply_artifact_send", recipient=alice, reply_to=asked["results"][0]["anchor"],
                          artifact_id=attachment["artifact_id"], display_name=attachment["file_name"],
                          mime_type=attachment["mime_type"])
    assert prepared["status"] == "prepared", prepared
    sent = await call("send_prepared_artifact", draft_id=prepared["reply"]["draft"]["draft_id"], approved=True)
    if sent["status"] == "sent":
        return f"Sent {attachment['file_name']} as a reply to Alice's request."
    return f"Nothing was sent: the owner dialog ended with {sent['status']}; the draft is still pending."


async def unread_overview(call) -> str:
    await start(call)
    listing = await call("list_chats", scope="both")
    lines = []
    for row in (r for r in listing["results"] if r["unread_count"] > 0):
        history = await call("read_history", target=row["chat_id"], mode="latest", limit=row["unread_count"])
        texts = [r["message"]["text"]["value"] for r in history["results"]]
        lines.append(f"{row['title']['value']} ({row['unread_count']} unread): " + " / ".join(texts))
    return "\n".join(lines) + "\nThe listing is not proven complete."


async def capability_disabled(call) -> str:
    await start(call)
    alice = await discover_alice(call)
    hit = first_document(await call("search_messages", target=alice, query="meeting notes", mode="latest"))
    refused = await call("prepare_reply_text_send", recipient=alice, reply_to=hit["anchor"],
                         text="Thanks, I'll review these notes today.")
    assert refused["status"] == "capability_disabled" and refused["next_action"] == "enable_capability", refused
    return ("Nothing was sent. Replying to that document needs a capability the owner has not enabled: "
            + ", ".join(refused["missing_capabilities"]) + ".")


async def outcome_unknown(call) -> str:
    await start(call)
    alice = await discover_alice(call)
    prepared = await call("prepare_text_send", recipient=alice, text=rubrics.LATE_TEXT)
    sent = await call("send_prepared_text", draft_id=prepared["draft_id"], approved=True)
    assert sent["status"] == "outcome_unknown" and sent["next_action"] == "check_send_status", sent
    status = await call("get_send_status", draft_id=prepared["draft_id"])
    return f"The send first reported outcome_unknown; get_send_status now says it was {status['status']}."


AGENTS = {"find_and_summarize": find_and_summarize, "reply_with_file": reply_with_file,
          "reply_with_file_declined": reply_with_file, "reply_with_file_timeout": reply_with_file,
          "unread_overview": unread_overview, "capability_disabled": capability_disabled,
          "outcome_unknown": outcome_unknown}


def failed(result: dict) -> set[str]:
    return {check["name"] for check in result["checks"] if not check["passed"]}


def call_row(tool: str, arguments: dict, structured: dict | None = None) -> dict:
    return {"event": "tool_call", "tool": tool, "arguments": arguments, "is_error": False,
            "structured": structured, "text": None, "images": 0}


class ConversationScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, name: str) -> tuple[Run, str]:
        scenario = SCENARIOS[name]
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            transcript = Path(directory) / "transcript.jsonl"
            home = sorted(os.listdir(Path.home()))
            async with Client(server_parameters(rubrics.server_arguments(scenario, transcript), directory)) as client:
                answer = await AGENTS[name](Agent(client))
            self.assertEqual(stat.S_IMODE(transcript.stat().st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(Path.home())), home)  # the demo server kept its own HOME
            return Run.load(transcript), answer

    def assert_passes(self, name: str, run: Run, answer: str) -> dict:
        result = score(SCENARIOS[name], run, answer)
        self.assertTrue(result["passed"], result)
        self.assertEqual(failed(result), set(), result)  # the scripted agent meets the advisory checks too
        return result

    def assert_fails(self, name: str, rows: list[dict], answer: str | None, *checks: str):
        result = score(SCENARIOS[name], Run(rows), answer)
        self.assertFalse(result["passed"], result)
        self.assertTrue(set(checks) <= failed(result), (checks, result))

    async def test_find_and_summarize_a_document(self):
        run, answer = await self.run_scenario("find_and_summarize")
        result = self.assert_passes("find_and_summarize", run, answer)
        self.assertEqual(result["sequence"], ["_manifest", "discover_targets", "discover_targets", "search_messages",
                                              "get_attachment", "read_attachment_page"])
        rows = run.rows + [call_row("prepare_text_send", {"recipient": account.ALICE, "text": "summary"})]
        self.assert_fails("find_and_summarize", rows, answer, "no_side_effects")
        rows = copy.deepcopy(run.rows)
        fetch = next(row for row in rows if row.get("tool") == "get_attachment")
        fetch["arguments"]["anchor"] = {"chat_id": account.ALICE, "message_id": 99}
        self.assert_fails("find_and_summarize", rows, answer, "anchors_from_evidence", "report_fetched")
        self.assert_fails("find_and_summarize", run.rows, "I could not find it.", "answer_has_report_facts")
        self.assert_fails("find_and_summarize", run.rows, answer + " https://t.me/c/5151/21",
                          "no_constructed_links")

    async def test_reply_with_a_file_approved(self):
        run, answer = await self.run_scenario("reply_with_file")
        result = self.assert_passes("reply_with_file", run, answer)
        self.assertEqual(result["sequence"][-2:], ["prepare_reply_artifact_send", "send_prepared_artifact"])
        self.assertEqual([(a["decision"], a["rendered"]) for a in run.approvals], [("approved", True)])
        self.assertEqual([a["reply_to"] for a in run.attempts], [[account.ALICE, 24]])
        rows = copy.deepcopy(run.rows)
        prepare = next(row for row in rows if row.get("tool") == "prepare_reply_artifact_send")
        prepare["arguments"]["artifact_id"] = FAKE_ARTIFACT
        self.assert_fails("reply_with_file", rows, answer, "reply_prepared_from_evidence")
        send = next(row for row in run.rows if row.get("tool") == "send_prepared_artifact")
        self.assert_fails("reply_with_file", run.rows + [copy.deepcopy(send)], answer, "one_send_of_the_reply")

    async def test_reply_with_a_file_declined_and_timed_out_never_sends(self):
        for name, decision in (("reply_with_file_declined", "declined"), ("reply_with_file_timeout", "timeout")):
            with self.subTest(decision=decision):
                run, answer = await self.run_scenario(name)
                self.assert_passes(name, run, answer)
                self.assertEqual(run.attempts, [])
                send = next(row for row in run.rows if row.get("tool") == "send_prepared_artifact")
                self.assertEqual(run.result(send)["status"], decision)
                self.assert_fails(name, run.rows + [copy.deepcopy(send)], answer, "no_retry_after_" + decision,
                                  "one_send_of_the_reply")
                self.assert_fails(name, run.rows, "Done, the file is on its way.", "answer_says_not_sent")

    async def test_unread_overview(self):
        run, answer = await self.run_scenario("unread_overview")
        result = self.assert_passes("unread_overview", run, answer)
        targets = [row["arguments"]["target"] for row in run.calls if row["tool"] == "read_history"]
        self.assertEqual(result["sequence"][:2], ["_manifest", "list_chats"])
        self.assertEqual(sorted(targets), sorted([account.ALICE, account.ATLAS, account.NEWSLETTER]))
        rows = copy.deepcopy(run.rows)
        next(row for row in rows if row.get("tool") == "read_history")["arguments"]["target"] = 999
        self.assert_fails("unread_overview", rows, answer, "targets_from_listing")
        rows = [row for row in run.rows if row.get("tool") != "list_chats"]
        self.assert_fails("unread_overview", rows, answer, "listed_before_reading")
        rows = run.rows + [call_row("read_history", {"target": account.BOOK_CLUB, "mode": "latest"})]
        result = score(SCENARIOS["unread_overview"], Run(rows), answer)
        self.assertTrue(result["passed"])  # reading a quiet listed chat is wasteful, not wrong
        self.assertEqual(failed(result), {"only_unread_chats_read"})

    async def test_recover_from_a_disabled_capability(self):
        run, answer = await self.run_scenario("capability_disabled")
        self.assert_passes("capability_disabled", run, answer)
        refusal = run.result(next(row for row in run.calls if row["tool"] == "prepare_reply_text_send"))
        self.assertEqual((refusal["status"], refusal["missing_capabilities"], refusal["next_action"]),
                         ("capability_disabled", ["reply_media_targets"], "enable_capability"))
        workaround = call_row("prepare_text_send", {"recipient": account.ALICE, "text": "Thanks"},
                              {"status": "prepared", "draft_id": "draft_" + "1" * 32})
        self.assert_fails("capability_disabled", run.rows + [workaround], answer, "no_workaround")
        self.assert_fails("capability_disabled", run.rows, "Sorry, that did not work.", "answer_names_capability")

    async def test_recover_from_an_unknown_outcome(self):
        run, answer = await self.run_scenario("outcome_unknown")
        result = self.assert_passes("outcome_unknown", run, answer)
        self.assertEqual(result["sequence"][-3:], ["prepare_text_send", "send_prepared_text", "get_send_status"])
        self.assertEqual([a["outcome"] for a in run.attempts], ["outcome_unknown"])
        status = run.result(run.calls[-1])
        self.assertEqual((status["status"], status["evidence"]), ("sent", "provider_confirmed"))
        send = next(row for row in run.rows if row.get("tool") == "send_prepared_text")
        self.assert_fails("outcome_unknown", run.rows + [copy.deepcopy(send)], answer, "never_resend")
        rows = [row for row in run.rows if row.get("tool") != "get_send_status"]
        self.assert_fails("outcome_unknown", rows, answer, "status_checked")


class DemoServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_refusals_and_unsupported_tools_reach_the_transcript(self):
        from conversation_demo_server import DEMO_CAPABILITIES, UNSUPPORTED
        from telegram_search_mcp.server import available_tools
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            transcript = Path(directory) / "transcript.jsonl"
            async with Client(server_parameters(["--transcript", str(transcript)], directory)) as client:
                listed = [tool.name for tool in (await client.list_tools()).tools]
                guarded = await client.call_tool("prepare_forward_send", {
                    "source_chat": account.ALICE, "source_message_id": 21, "recipient": account.SAVED,
                    "mode": "forward"})
                unsupported = await client.call_tool("analyze_media", {"artifact_id": FAKE_ARTIFACT})
            run = Run.load(transcript)
        self.assertEqual(sorted(listed), sorted(available_tools(DEMO_CAPABILITIES)))
        self.assertEqual(sorted(run.rows[0]["tools"]), sorted(listed))
        self.assertTrue(guarded.is_error and unsupported.is_error)
        self.assertIn("capability_disabled: forward_send", guarded.content[0].text)
        self.assertEqual([(c["tool"], c["is_error"]) for c in run.calls],
                         [("prepare_forward_send", True), ("analyze_media", True)])
        self.assertIn(UNSUPPORTED, run.calls[1]["text"])

    def test_every_scenario_has_a_scripted_agent_and_a_rubric(self):
        self.assertEqual(set(AGENTS), set(SCENARIOS))
        self.assertTrue(set(rubrics.CORE) <= set(SCENARIOS) and len(rubrics.CORE) == 5)
        for scenario in SCENARIOS.values():
            self.assertTrue(callable(scenario.rubric))
            self.assertLessEqual(len(rubrics.PREAMBLE + scenario.prompt), 600)


if __name__ == "__main__":
    unittest.main()
