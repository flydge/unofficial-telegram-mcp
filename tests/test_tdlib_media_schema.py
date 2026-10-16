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
