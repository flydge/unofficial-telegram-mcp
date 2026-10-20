"""Compare outgoing media shapes with the installed, pinned TDLib header."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from telegram_search_mcp.config import TDLIB_LIBRARY
from telegram_search_mcp.tdjson import ForbiddenTDLibRequest, TDLibClient


class PinnedMediaSchemaTests(unittest.TestCase):
    def test_outgoing_media_matches_native_constructor_fields(self) -> None:
        header_path = TDLIB_LIBRARY.resolve().parent.parent / "include/td/telegram/td_api.h"
        if not header_path.is_file():
            self.skipTest("pinned TDLib development header is not installed")
        header = header_path.read_text()

        def fields(class_name: str) -> dict[str, str]:
            declaration = header.split(f"class {class_name} final", 1)[1].split("\n};", 1)[0]
            return {name: kind for kind, name in re.findall(r"^\s+([^\n]+?)\s+(\w+)_;$", declaration, re.MULTILINE)}

        client = TDLibClient(raw=Mock())
        path = Path.home() / "Library/Application Support/TelegramSearchMCP/outgoing-staging/draft_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/media.bin"
        def video(chat_id, path, caption):
            # The exact content send_video_message builds after its account/recipient revalidation.
            return client._send_message_content(chat_id, TDLibClient._video_content(path, caption, duration_seconds=1,
                width=16, height=16, supports_streaming=True), "messageVideo")
        for field, method, extra in (
            ("document", client.send_document_message, ()),
            ("photo", client.send_photo_message, ()),
            ("voice_note", client.send_voice_note_message, (1, "A" * 84)),
            ("video", video, ()),
        ):
            with self.subTest(field=field), patch.object(client, "_send_message_content", return_value=1) as send:
                method(123, path, "", *extra)
                content = send.call_args.args[1]
                outer_fields = fields(content["@type"])
                self.assertEqual(set(content) - {"@type"}, set(outer_fields))
                nested_type = re.fullmatch(r"object_ptr<(\w+)>", outer_fields[field]).group(1)
                self.assertEqual(content[field]["@type"], nested_type)
                self.assertEqual(set(content[field]) - {"@type"}, set(fields(nested_type)))

    def test_archive_and_text_edit_requests_match_native_constructor_fields(self) -> None:
        header_path = TDLIB_LIBRARY.resolve().parent.parent / "include/td/telegram/td_api.h"
        if not header_path.is_file():
            self.skipTest("pinned TDLib development header is not installed")
        header = header_path.read_text()

        def fields(class_name: str) -> dict[str, str]:
            declaration = header.split(f"class {class_name} final", 1)[1].split("\n};", 1)[0]
            return {name: kind for kind, name in re.findall(r"^\s+([^\n]+?)\s+(\w+)_;$", declaration, re.MULTILINE)}

        from telegram_search_mcp.archive_drafts import ArchiveSnapshot
        from telegram_search_mcp.edit_drafts import EditSnapshot
        client = TDLibClient(raw=Mock())
        archive = ArchiveSnapshot(chat_id=-100, identity=("chatTypeSupergroup", 1, False), title="T",
                                  kind="supergroup", current_list="main")
        edit = Mock(spec=EditSnapshot, anchor=(-100, 1048576), can_be_edited=True, raw_text="old", edit_date=0)
        message = {"@type": "message", "id": 1048576, "chat_id": -100, "edit_date": 5,
                   "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": "new", "entities": []}}}
        requests = []
        def transport(payload, **_):
            requests.append(payload)
            return {"@type": "ok"} if payload["@type"] == "addChatToList" else message
        with patch.object(client, "_change_transport", side_effect=transport), \
                patch.object(client, "get_account_id", return_value=7), \
                patch.object(client, "read_chat_list_state", return_value=archive), \
                patch.object(client, "read_own_text_message", return_value=edit), \
                patch.object(client, "_observe_chat_list", return_value="archive"):
            client.move_chat_to_list(-100, target_list="archive", expected=archive, expected_account_id=7,
                                     pre_send_guard=lambda: True)
            client.edit_own_text(-100, 1048576, new_text="new", expected=edit, expected_account_id=7,
                                 pre_send_guard=lambda: True)
        add, edit_request = requests
        self.assertEqual(set(add) - {"@type"}, set(fields("addChatToList")))
        self.assertEqual(fields("addChatToList")["chat_list"], "object_ptr<ChatList>")
        self.assertIn(f"class {add['chat_list']['@type']} final : public ChatList", header)
        self.assertEqual(fields(add["chat_list"]["@type"]), {})
        self.assertEqual(set(edit_request) - {"@type"}, set(fields("editMessageText")))
        content = edit_request["input_message_content"]
        self.assertEqual(fields("editMessageText")["input_message_content"], "object_ptr<InputMessageContent>")
        self.assertIn(f"class {content['@type']} final : public InputMessageContent", header)
        self.assertEqual(set(content) - {"@type"}, set(fields(content["@type"])))
        self.assertEqual(fields(content["@type"])["text"], f"object_ptr<{content['text']['@type']}>")
        self.assertEqual(set(content["text"]) - {"@type"}, set(fields(content["text"]["@type"])))
        # The verification reads: chat positions and the message edit date and edit permission.
        self.assertIn("positions", fields("chat"))
        self.assertEqual(set(fields("chatPosition")), {"list", "order", "is_pinned", "source"})
        self.assertEqual(fields("message")["edit_date"], "int32")
        self.assertEqual(fields("messageProperties")["can_be_edited"], "bool")

    def test_chat_list_reads_match_native_constructor_fields(self) -> None:
        header_path = TDLIB_LIBRARY.resolve().parent.parent / "include/td/telegram/td_api.h"
        if not header_path.is_file():
            self.skipTest("pinned TDLib development header is not installed")
        header = header_path.read_text()

        def fields(class_name: str) -> dict[str, str]:
            declaration = header.split(f"class {class_name} final", 1)[1].split("\n};", 1)[0]
            return {name: kind for kind, name in re.findall(r"^\s+([^\n]+?)\s+(\w+)_;$", declaration, re.MULTILINE)}

        from telegram_search_mcp.tdjson import MessageNotFound, TDLibError, _validate_bounded_request
        client = TDLibClient(raw=Mock())
        chat = {"@type": "chat", "id": -100, "title": "T", "positions": [],
                "type": {"@type": "chatTypeSupergroup", "supergroup_id": 1, "is_channel": False}}
        sent: list[dict] = []
        unknown = TDLibError("TDLib request failed with code 400")
        unknown.provider_code = 400
        answers = {"getChat": [unknown, chat],
                   "loadChats": [{"@type": "ok"}, MessageNotFound("end")],
                   "getChatListsToAddChat": [{"@type": "chatLists", "chat_lists": [
                       {"@type": "chatListMain"}, {"@type": "chatListArchive"},
                       {"@type": "chatListFolder", "chat_folder_id": 3}]}]}

        def call(payload):
            _validate_bounded_request(payload)
            sent.append(payload)
            answer = answers[payload["@type"]].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with patch.object(client, "_call", side_effect=call):
            client.read_chat_list_state(-100)
            self.assertEqual(client.chat_lists_to_add(-100), frozenset({"main", "archive"}))
        self.assertEqual([request["@type"] for request in sent],
                         ["getChat", "loadChats", "loadChats", "getChat", "getChatListsToAddChat"])
        for request in sent:
            self.assertEqual(set(request) - {"@type"}, set(fields(request["@type"])))
        self.assertEqual(fields("loadChats"), {"chat_list": "object_ptr<ChatList>", "limit": "int32"})
        self.assertEqual({request["chat_list"]["@type"] for request in sent[1:3]}, {"chatListMain", "chatListArchive"})
        for request in sent[1:3]:
            self.assertIn(f"class {request['chat_list']['@type']} final : public ChatList", header)
        self.assertEqual(fields("getChatListsToAddChat"), {"chat_id": "int53"})
        self.assertIn("class getChatListsToAddChat final : public Function", header)
        self.assertIn("using ReturnType = object_ptr<chatLists>;", header.split("class getChatListsToAddChat final", 1)[1])
        self.assertEqual(fields("chatLists"), {"chat_lists": "array<object_ptr<ChatList>>"})
        for kind in ("chatListMain", "chatListArchive", "chatListFolder"):
            self.assertIn(f"class {kind} final : public ChatList", header)
        self.assertEqual(fields("chatListMain"), {})

    def test_media_rejects_files_outside_approved_staging(self) -> None:
        raw = Mock()
        client = TDLibClient(raw=raw)
        client._ready = True
        def send_video_message(chat_id, path, caption):
            return client._send_message_content(chat_id, TDLibClient._video_content(path, caption, duration_seconds=1,
                width=16, height=16, supports_streaming=True), "messageVideo", video=(1, 16, 16, 1))
        for method, extra in (
            (client.send_document_message, ()),
            (client.send_photo_message, ()),
            (client.send_voice_note_message, (1, "A" * 84)),
            (send_video_message, ()),
        ):
            with self.subTest(method=method.__name__), self.assertRaises(ForbiddenTDLibRequest):
                method(123, Path("/tmp/unapproved-file"), "", *extra)
        raw.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
