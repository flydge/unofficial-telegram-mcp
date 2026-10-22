"""Conversation harness through the official SDK (0.54 item E, criterion 10): deterministic, no model.

Each scenario runs a scripted agent through the official MCP SDK client against the demo server
(the real server over STDIO, a synthetic account, scripted approvals) and scores the server's own
transcript with the rubric that `conversation_agent_run.py` applies to real agents. Every argument
the scripted agent passes is read out of an earlier tool result. Mutated copies of the same
transcripts prove that each rubric fails the behaviour it forbids.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters

import conversation_account as account
import conversation_agent_run as agent_run
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
    if status["status"] == "outcome_unknown":
        return ("The send reported outcome_unknown and get_send_status still reports outcome_unknown, so whether "
                "Alice received it is unknown. I did not resend it.")
    return f"The send first reported outcome_unknown; get_send_status now says it was {status['status']}."


AGENTS = {"find_and_summarize": find_and_summarize, "reply_with_file": reply_with_file,
          "reply_with_file_declined": reply_with_file, "reply_with_file_timeout": reply_with_file,
          "unread_overview": unread_overview, "capability_disabled": capability_disabled,
          "outcome_unknown": outcome_unknown, "outcome_unknown_unresolved": outcome_unknown}
# The answer check each scenario's final answer must pass; a missing answer fails it.
ANSWER_CHECKS = {"find_and_summarize": "answer_has_report_facts", "reply_with_file": "answer_reports_sent",
                 "reply_with_file_declined": "answer_says_not_sent", "reply_with_file_timeout": "answer_says_not_sent",
                 "unread_overview": "answer_names_unread_chats", "capability_disabled": "answer_claims_no_send",
                 "outcome_unknown": "answer_reports_status", "outcome_unknown_unresolved": "answer_says_unknown"}
# Answers that contradict a sent outcome, each in a form the old closed negation list let through.
NOT_SENT_ANSWERS = ("Nothing was sent.", "The message wasn\u2019t sent.", "It was neither sent nor delivered.",
                    "I was unable to confirm it was sent.", "I replied to Alice, but the file was not sent.",
                    "Was it sent? I cannot tell.", "I think it was probably sent.", "It was sent, I believe.")
# Answers to a declined or timed-out dialog that still claim delivery.
DELIVERY_CLAIMS = ("Done: the file was sent to Alice; the owner dialog was not declined.",
                   "Sent it. The dialog wasn't declined.", "The owner declined nothing; I sent the file to Alice.",
                   "The dialog never timed out, so I sent it.", "Sent the file; no timeout occurred.",
                   "Nothing was sent at first, but then the file went through.")


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

    def assert_answer_required(self, name: str, run: Run):
        """A run with no final answer (an agent that hung, crashed or ran out of turns) fails the answer check."""
        for answer in (None, "", "  \n"):
            with self.subTest(scenario=name, answer=answer):
                self.assert_fails(name, run.rows, answer, ANSWER_CHECKS[name])
                result = score(SCENARIOS[name], run, answer)
                check = next(c for c in result["checks"] if c["name"] == ANSWER_CHECKS[name])
                self.assertEqual((check["required"], check["detail"]), (True, "no final answer"))

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
        self.assert_answer_required("find_and_summarize", run)
        self.assert_fails("find_and_summarize", run.rows, None, "no_constructed_links")

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
        for wrong in NOT_SENT_ANSWERS:
            with self.subTest(answer=wrong):
                self.assert_fails("reply_with_file", run.rows, wrong, "answer_reports_sent")
        for right in ("Replied to Alice\u2019s budget request with `Budget-2026.xlsx` from Saved Messages.",
                      "Done \u2014 the spreadsheet was delivered as a reply to Alice."):
            with self.subTest(answer=right):
                self.assertTrue(score(SCENARIOS["reply_with_file"], run, right)["passed"])
        self.assert_answer_required("reply_with_file", run)

    def test_status_claims_count_only_as_plain_statements(self):
        """A status claim counts only as an explicit statement, never negated, hedged or asked; a denial is a send
        verb right after a negation; a forbidden claim counts wherever it is not negated or hedged."""
        for text in ("It was sent at 14:05.", "Sent \u2014 I did not resend it.", "I didn't resend, since it was sent.",
                     "Sent Budget-2026.xlsx as a reply.", "Sent without errors.",
                     "The tool reports Budget-2026.xlsx sent to Alice Example as a reply, without a caption."):
            with self.subTest(affirmed=text):
                self.assertTrue(rubrics.affirms(text, rubrics.SENT))
        for text in tuple(a for a in NOT_SENT_ANSWERS if "replied" not in a) + (
                "It was probably not sent.", "Unsent so far.", "The message was resent.", "It was sent, not delivered.",
                "Was it sent?", "If it was sent, Alice has it.", "It wasnt sent.",
                "It was sent, which I couldn't verify.", "With no errors, the file was sent."):
            with self.subTest(not_affirmed=text):
                self.assertFalse(rubrics.affirms(text, rubrics.SENT))
        for text in ("Nothing was sent.", "The message wasn\u2019t sent.", "It was neither sent nor delivered.",
                     "No message to Alice was sent.", "The dialog timed out without a choice; no attempt was made.",
                     "It stays unsent."):
            with self.subTest(denied=text):
                self.assertTrue(rubrics.denies(text, rubrics.NOT_SENT, words=rubrics.UNSENT))
        for text in ("Probably not sent.", "I did not send it again.", "I didn't resend it.", "It was sent.",
                     "This does not confirm a real Telegram send.", "I cannot confirm that nothing was sent."):
            with self.subTest(not_denied=text):
                self.assertFalse(rubrics.denies(text, rubrics.NOT_SENT, words=rubrics.UNSENT))
        for text in DELIVERY_CLAIMS:
            with self.subTest(claimed=text):
                self.assertTrue(rubrics.claims(text, rubrics.SENT))
        for text in ("Nothing was sent.", "It was neither sent nor delivered.", "Was it sent?",
                     "It is unknown whether it was sent."):
            with self.subTest(not_claimed=text):
                self.assertFalse(rubrics.claims(text, rubrics.SENT))
        self.assertTrue(rubrics.states("Delivery is not confirmed yet.", rubrics.UNKNOWN))
        self.assertTrue(rubrics.states("The outcome is `outcome_unknown`.", rubrics.UNKNOWN))
        self.assertFalse(rubrics.states("The outcome is no longer unknown.", rubrics.UNKNOWN))
        self.assertFalse(rubrics.states("Is it unknown?", rubrics.UNKNOWN))
        self.assertTrue(rubrics.denies("I won\u2019t send it again.", rubrics.RESEND))
        self.assertTrue(rubrics.denies("No resend was made.", rubrics.RESEND))
        self.assertFalse(rubrics.denies("I resent it.", rubrics.RESEND))
        self.assertTrue(rubrics.mentions("The report covers telemetry.", "telemetry"))
        self.assertFalse(rubrics.mentions("The report covers telemetry-free data.", "telemetry"))

    async def test_the_send_must_be_the_draft_prepared_from_the_right_evidence(self):
        run, answer = await self.run_scenario("reply_with_file")
        prepare = next(row for row in run.rows if row.get("tool") == "prepare_reply_artifact_send")
        draft = rubrics._prepared_draft(run, prepare)
        other = "draft_" + "e" * 32
        # A second reply draft (to another message) is prepared, and the send uses it instead.
        second = json.loads(json.dumps(prepare).replace(draft, other))
        second["arguments"]["reply_to"] = {"anchor": {"chat_id": account.ALICE, "message_id": 21}}
        rows = []
        for row in run.rows:
            if row.get("tool") == "send_prepared_artifact" or row.get("event") in {"approval", "send_attempt"}:
                row = json.loads(json.dumps(row).replace(draft, other))
                if row.get("event") == "send_attempt":
                    row["reply_to"] = [account.ALICE, 21]
            rows.append(row)
            if row is not None and row.get("tool") == "prepare_reply_artifact_send":
                rows.append(second)
        self.assert_fails("reply_with_file", rows, answer, "one_send_of_the_reply", "attempts_are_the_reply")

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
                for wrong in DELIVERY_CLAIMS + ("I think the file probably reached Alice.", "Was it sent? Unclear."):
                    with self.subTest(decision=decision, answer=wrong):
                        self.assert_fails(name, run.rows, wrong, "answer_says_not_sent")
                detail = run.result(send)["detail"]  # the server's own fixed text, quoted back
                for right in (f"The owner dialog said: {detail}.", "Nothing was sent; the draft is still pending.",
                              "I didn\u2019t send the file to Alice."):
                    with self.subTest(decision=decision, answer=right):
                        self.assertTrue(score(SCENARIOS[name], run, right)["passed"], right)
                self.assert_answer_required(name, run)

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
        for wrong in ("I enabled reply_media_targets and sent the reply to Alice.",
                      "reply_media_targets was off, so I replied to Alice anyway."):
            with self.subTest(answer=wrong):
                self.assert_fails("capability_disabled", run.rows, wrong, "answer_claims_no_send")
        self.assert_answer_required("capability_disabled", run)
        self.assert_fails("capability_disabled", run.rows, None, "answer_names_capability")
        # Malformed calls the server refused (as a real agent made them) are noise, not invented anchors or
        # retries, unless an anchor inside one was never returned; retrying the refusal itself is a loop.
        refused_at = run.rows.index(next(row for row in run.rows if row.get("tool") == "prepare_reply_text_send"))

        def malformed(message_id):
            return {**call_row("prepare_reply_text_send", {"reply_to": {"anchor": {
                "chat_id": account.ALICE, "message_id": message_id}}, "text": "Thanks"}), "is_error": True,
                "text": "Error executing tool prepare_reply_text_send: invalid_arguments: missing recipient"}
        rows = run.rows[:refused_at] + [malformed(23)] + run.rows[refused_at:]
        result = score(SCENARIOS["capability_disabled"], Run(rows), answer)
        self.assertTrue(result["passed"], result)
        self.assertEqual(failed(result), {"argument_errors"})
        rows = run.rows[:refused_at] + [malformed(99)] + run.rows[refused_at:]
        self.assert_fails("capability_disabled", rows, answer, "anchors_from_evidence")
        refusal_row = run.rows[refused_at]
        self.assert_fails("capability_disabled", run.rows + [copy.deepcopy(refusal_row)] * 2, answer, "no_workaround")
        # Any reply prepared after the refusal, whatever its anchor, is a workaround.
        retarget = copy.deepcopy(refusal_row)
        retarget["arguments"]["reply_to"] = {"anchor": {"chat_id": account.ALICE, "message_id": 21}}
        self.assert_fails("capability_disabled", run.rows + [retarget], answer, "no_workaround")
        # Stopping before the call, from _manifest (the capability is off) and the tool description, is the
        # same recovery when the answer names the capability; without that answer it is not shown.
        manifest_only = [row for row in run.rows if row.get("event") != "tool_call" or row["tool"] == "_manifest"]
        result = score(SCENARIOS["capability_disabled"], Run(manifest_only), answer)
        self.assertTrue(result["passed"], result)
        self.assertEqual(failed(result), {"refusal_seen"})
        self.assert_fails("capability_disabled", manifest_only, None, "stopped_at_capability")
        self.assert_fails("capability_disabled", manifest_only, "I cannot do that.", "stopped_at_capability",
                          "answer_names_capability")

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
        # The right text is prepared, but a different draft (a longer text) is the one sent.
        prepare = next(row for row in run.rows if row.get("tool") == "prepare_text_send")
        draft = rubrics._prepared_draft(run, prepare)
        other = "draft_" + "d" * 32
        second = json.loads(json.dumps(prepare).replace(draft, other))
        second["arguments"]["text"] = rubrics.LATE_TEXT + " Sorry!"
        rows = []
        for row in run.rows:
            if row.get("tool") in {"send_prepared_text", "get_send_status"} or row.get("event") in {"approval",
                                                                                                "send_attempt"}:
                row = json.loads(json.dumps(row).replace(draft, other))
            rows.append(row)
            if row.get("tool") == "prepare_text_send":
                rows.append(second)
        self.assert_fails("outcome_unknown", rows, answer, "one_send_then_unknown")
        rows = copy.deepcopy(run.rows)
        next(row for row in rows if row.get("tool") == "prepare_text_send")["arguments"]["text"] += " Sorry!"
        self.assert_fails("outcome_unknown", rows, answer, "text_prepared_exactly")
        self.assert_fails("outcome_unknown", run.rows, "Probably not sent. Unconfirmed.", "answer_reports_status")
        for wrong in NOT_SENT_ANSWERS:
            with self.subTest(answer=wrong):
                self.assert_fails("outcome_unknown", run.rows, wrong, "answer_reports_status")
        for right in ("Sent to Alice Example: \u201cRunning ten minutes late, start without me.\u201d",
                      "I didn't send it again; get_send_status confirms it was sent."):
            with self.subTest(answer=right):
                self.assertTrue(score(SCENARIOS["outcome_unknown"], run, right)["passed"], right)
        self.assert_answer_required("outcome_unknown", run)

    async def test_an_outcome_that_stays_unknown_is_reported_as_unknown_and_never_resent(self):
        run, answer = await self.run_scenario("outcome_unknown_unresolved")
        result = self.assert_passes("outcome_unknown_unresolved", run, answer)
        self.assertEqual(result["sequence"][-3:], ["prepare_text_send", "send_prepared_text", "get_send_status"])
        self.assertEqual([(a["outcome"], a["delivered"]) for a in run.attempts], [("outcome_unknown", False)])
        status = run.result(run.calls[-1])
        self.assertEqual((status["status"], status["evidence"]), ("outcome_unknown", "provider_pending"))
        send = next(row for row in run.rows if row.get("tool") == "send_prepared_text")
        self.assert_fails("outcome_unknown_unresolved", run.rows + [copy.deepcopy(send)], answer, "never_resend")
        rows = [row for row in run.rows if row.get("tool") != "get_send_status"]
        self.assert_fails("outcome_unknown_unresolved", rows, answer, "status_checked", "status_stayed_unknown")
        # The resolved scenario's transcript (get_send_status says sent) is not this scenario's evidence.
        resolved, _ = await self.run_scenario("outcome_unknown")
        self.assert_fails("outcome_unknown_unresolved", resolved.rows, answer, "status_stayed_unknown")
        for wrong, checks in (
                ("Done: your message was sent and delivered to Alice.", ("answer_says_unknown",
                                                                        "answer_claims_no_delivery")),
                ("Your message to Alice was sent.", ("answer_says_unknown", "answer_says_not_resent")),
                ("The outcome is unknown.", ("answer_says_not_resent",)),
                ("The outcome is unknown, so I sent it again.", ("answer_says_not_resent",)),
                ("It was delivered after all; the first result was unknown and I did not resend.",
                 ("answer_claims_no_delivery",)),
                ("The outcome is no longer unknown: Alice received it. I did not resend.",
                 ("answer_says_unknown", "answer_claims_no_delivery")),
                ("Is the outcome unknown? I did not resend.", ("answer_says_unknown",))):
            with self.subTest(answer=wrong):
                self.assert_fails("outcome_unknown_unresolved", run.rows, wrong, *checks)
        for right in ("Delivery is still unconfirmed (get_send_status: outcome_unknown), and I did not resend it.",
                      "I sent it once, but the outcome is unknown; I won\u2019t send it again automatically.",
                      "The outcome is unknown, so I didn't retry."):
            with self.subTest(answer=right):
                self.assertTrue(score(SCENARIOS["outcome_unknown_unresolved"], run, right)["passed"], right)
        self.assert_answer_required("outcome_unknown_unresolved", run)
        for name in ("answer_says_not_resent", "answer_claims_no_delivery"):
            self.assert_fails("outcome_unknown_unresolved", run.rows, None, name)


# A stand-in for the agent process: it writes a recorded transcript and answer, prints events, then exits
# with a chosen code or sleeps past the deadline.
REPLAY = """
import json, pathlib, sys, time
plan = json.loads(sys.argv[1])
pathlib.Path(plan["transcript"]).write_text("".join(json.dumps(row) + "\\n" for row in plan["rows"]))
if plan["answer"] is not None:
    pathlib.Path(plan["answer_path"]).write_text(plan["answer"])
for event in plan["events"]:
    print(json.dumps(event), flush=True)
time.sleep(plan["sleep"])
sys.exit(plan["code"])
"""
COMPLETED = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}, {"type": "turn.completed"}]


class ReplayCodex(agent_run.Codex):
    """The runner's real Codex answer and event parsing over a replayed run."""

    def __init__(self, rows, *, answer, events=COMPLETED, code=0, sleep=0.0):
        super().__init__(sys.executable, None, None)
        self.plan = {"rows": rows, "answer": answer, "events": list(events), "code": code, "sleep": sleep}

    def command(self, scenario, transcript, workdir):
        plan = {**self.plan, "transcript": str(transcript), "answer_path": str(workdir / "answer.txt")}
        return [sys.executable, "-I", "-c", REPLAY, json.dumps(plan)], {"PATH": os.environ.get("PATH", "")}


class AgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    """The real-agent runner fails a scenario on a timeout, a non-zero exit, a failed turn or no answer."""

    async def asyncSetUp(self):
        scenario = SCENARIOS["outcome_unknown_unresolved"]
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            transcript = Path(directory) / "transcript.jsonl"
            async with Client(server_parameters(rubrics.server_arguments(scenario, transcript), directory)) as client:
                self.answer = await AGENTS[scenario.name](Agent(client))
            self.rows = Run.load(transcript).rows

    def run_replay(self, agent, timeout=60.0) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            return agent_run.run_scenario(agent, SCENARIOS["outcome_unknown_unresolved"], Path(directory), timeout)

    def test_a_clean_run_passes_and_every_failure_mode_fails(self):
        clean = self.run_replay(ReplayCodex(self.rows, answer=self.answer))
        self.assertTrue(clean["passed"], clean)
        self.assertEqual((clean["exit"], clean["timed_out"]), (0, False))
        self.assertEqual(failed(clean), set())
        cases = {
            "nonzero_exit": (ReplayCodex(self.rows, answer=self.answer, code=1), 60.0, {"agent_exited_cleanly"}),
            "timeout": (ReplayCodex(self.rows, answer=self.answer, sleep=30), 3.0, {"agent_exited_cleanly"}),
            "turn_failed": (ReplayCodex(self.rows, answer=self.answer, events=COMPLETED[:2] + [
                {"type": "turn.failed", "error": {"message": "synthetic"}}]), 60.0, {"turn_completed"}),
            "no_turn_completed": (ReplayCodex(self.rows, answer=self.answer, events=COMPLETED[:2]), 60.0,
                                  {"turn_completed"}),
            "no_answer": (ReplayCodex(self.rows, answer=None), 60.0, {"answer_says_unknown", "answer_says_not_resent"}),
        }
        for label, (agent, timeout, checks) in cases.items():
            with self.subTest(case=label):
                result = self.run_replay(agent, timeout)
                self.assertFalse(result["passed"], result)
                self.assertTrue(checks <= failed(result), (checks, result["checks"]))
                self.assertTrue(all(c["required"] for c in result["checks"] if c["name"] in checks))
        # A transient Codex error event (a reconnect) during a completed turn is not a failure.
        noisy = COMPLETED[:2] + [{"type": "error", "message": "Reconnecting... 1/5"}] + COMPLETED[2:]
        self.assertTrue(self.run_replay(ReplayCodex(self.rows, answer=self.answer, events=noisy))["passed"])

    def test_claude_turn_results_are_read_from_the_result_event(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            for lines, completed in (([{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}],
                                      True),
                                     ([{"type": "result", "subtype": "error_max_turns", "is_error": True}], False),
                                     ([{"type": "result", "subtype": "success", "is_error": True, "result": "x"}],
                                      False),
                                     ([{"type": "assistant", "message": {"content": []}}], False)):
                events.write_text("".join(json.dumps(line) + "\n" for line in lines))
                with self.subTest(lines=lines):
                    observed = agent_run.ClaudeCode.observations(events)
                    self.assertEqual(observed["turn_completed"], completed)


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
