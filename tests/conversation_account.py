"""Synthetic Telegram account for the conversation harness (0.54 item E); stdlib only.

Every name, identifier, link and text below is invented. The demo server serves these facts through
the real MCP surface, and the rubrics and the agent runner read the same constants, so an expected
anchor or file never drifts from what the server returns. Identifiers stay small and supergroup ids
short so that the public-tree privacy rules never mistake them for real ones.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ACCOUNT_ID = 4242
USERS = {4242: "Demo Owner", 5151: "Alice Example", 6161: "Bob Example", 6262: "Carol Example",
         7373: "Dana Example"}

SAVED, ALICE, ATLAS, BOOK_CLUB, NEWSLETTER = 4242, 5151, -1007001, -1007002, -1007003
# The newest synthetic message is 20 minutes before this instant.
BASE = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Chat:
    chat_id: int
    title: str
    kind: str  # self, private, supergroup or channel
    chat_list: str  # main or archive
    username: str | None = None
    forum: bool = False
    topics: tuple[tuple[int, str], ...] = ()
    link_id: int | None = None  # provider links https://t.me/c/<link_id>/<message_id>


CHATS = (
    Chat(ATLAS, "Project Atlas", "supergroup", "main", forum=True,
         topics=((1, "General"), (2, "Design"), (3, "Releases")), link_id=7001),
    Chat(ALICE, "Alice Example", "private", "main", username="alice_example"),
    Chat(SAVED, "Saved Messages", "self", "main"),
    Chat(BOOK_CLUB, "Book Club", "supergroup", "main", link_id=7002),
    Chat(NEWSLETTER, "Atlas Newsletter", "channel", "archive", link_id=7003),
)
CHAT_BY_ID = {chat.chat_id: chat for chat in CHATS}


@dataclass(frozen=True)
class Document:
    key: str
    file_name: str
    mime_type: str
    kind: str  # pdf, xlsx, pptx or docx
    parts: tuple  # pages (lines), sheet rows, slide titles or paragraphs
    facts: tuple[str, ...]  # what a faithful summary carries


DOCUMENTS = {
    "report": Document("report", "Quarterly-Report-Q3.pdf", "application/pdf", "pdf", (
        ("Project Atlas - Quarterly Report Q3 2026", "Revenue: 4.2 million credits, up 12% on Q2.",
         "Active users: 18,400."),
        ("Risks: the telemetry vendor is two weeks late.", "Next step: release 1.4 on November 3."),
    ), ("12%", "18,400", "telemetry")),
    "budget": Document("budget", "Budget-2026.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx", (
        ("Category", "Q1", "Q2", "Q3", "Q4"),
        ("Engineering", 120, 130, 140, 150),
        ("Marketing", 40, 45, 50, 55),
        ("Operations", 30, 30, 35, 35),
        ("Total", 190, 205, 225, 240),
    ), ("Engineering", "240")),
    "roadmap": Document("roadmap", "Atlas-Roadmap.pptx",
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx", (
        "Atlas Roadmap v2", "Milestone: telemetry beta in December", "Milestone: public launch in March",
    ), ("telemetry beta", "March")),
    "notes": Document("notes", "Meeting-notes.docx",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", (
        "Meeting notes - Tuesday", "Decision: adopt the new onboarding flow.", "Owner: Bob Example. Due: next sprint.",
    ), ("onboarding flow",)),
}


@dataclass(frozen=True)
class Message:
    chat_id: int
    message_id: int
    sender_id: int  # a user id, or the channel's own chat id for a channel post
    minutes: int  # minutes before BASE
    text: str  # message text, or the caption of a document
    outgoing: bool = False
    document: str | None = None  # key into DOCUMENTS
    topic: int | None = None  # forum topic id
    unread: bool = False

    @property
    def date(self) -> datetime:
        return BASE - timedelta(minutes=self.minutes)


MESSAGES = (
    Message(SAVED, 11, 4242, 3 * 1440, "Reading list for the Atlas launch: https://example.org/atlas-notes",
            outgoing=True),
    Message(SAVED, 12, 4242, 2 * 1440, "Budget 2026 spreadsheet (final)", outgoing=True, document="budget"),
    Message(SAVED, 13, 4242, 1440, "Remember: send Alice the budget spreadsheet before Friday.", outgoing=True),
    Message(ALICE, 21, 5151, 3 * 1440, "Quarterly report Q3 for Project Atlas", document="report"),
    Message(ALICE, 22, 4242, 3 * 1440 - 10, "Thanks, I will read it today.", outgoing=True),
    Message(ALICE, 23, 5151, 2 * 1440, "Notes from Tuesday's meeting", document="notes"),
    Message(ALICE, 24, 5151, 60, "Could you send me the budget spreadsheet when you have a minute?", unread=True),
    Message(ALICE, 25, 5151, 30, "Also, are we still on for lunch on Thursday?", unread=True),
    Message(ATLAS, 31, 6262, 4 * 1440, "Welcome to Project Atlas! Kickoff notes are in the Design topic.", topic=1),
    Message(ATLAS, 32, 6161, 2 * 1440, "Roadmap deck v2 for review", document="roadmap", topic=2),
    Message(ATLAS, 33, 6262, 90, "Looks good. @alice_example please check slide 2.", topic=2, unread=True),
    Message(ATLAS, 34, 6161, 80, "Release 1.4 is scheduled for November 3. Changelog: "
            "https://example.org/atlas/changelog", topic=3, unread=True),
    Message(ATLAS, 35, 6262, 20, "Reminder: the code freeze starts on Friday.", topic=3, unread=True),
    Message(BOOK_CLUB, 41, 7373, 5 * 1440, "Next meeting on Sunday evening; bring your favourite quote."),
    Message(NEWSLETTER, 51, NEWSLETTER, 1440, "Issue 12: community highlights and the new Atlas logo.", unread=True),
)


def unread_count(chat_id: int) -> int:
    return sum(1 for message in MESSAGES if message.chat_id == chat_id and message.unread)


def message(chat_id: int, message_id: int) -> Message | None:
    return next((m for m in MESSAGES if (m.chat_id, m.message_id) == (chat_id, message_id)), None)


def document_message(key: str) -> Message:
    return next(m for m in MESSAGES if m.document == key)
