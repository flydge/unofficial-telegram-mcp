from __future__ import annotations

import asyncio
import importlib.util
import importlib.machinery
import hashlib
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def result(text, *, error=True, structured=None, count=1):
    return SimpleNamespace(is_error=error, structured_content=structured,
                           content=[SimpleNamespace(type="text", text=text)] * count)


class ReleaseSDKProbeTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("telegram_search_mcp.release_sdk_probe"),
                             "tracked installed SDK probe is missing")
        from telegram_search_mcp import release_sdk_probe
        return release_sdk_probe

    def test_masked_and_detailed_channels_match_only_the_expected_tool(self):
        module = self.module()
        module.check_error(result("Error executing tool update_draft"),
                           "update_draft", "Error executing tool update_draft")
        module.check_error(result("Error executing tool update_reply_draft: exact model detail"),
                           "update_reply_draft", "Error executing tool update_reply_draft: exact model detail")
        with self.assertRaises(module.ProbeError):
            module.check_error(result("Error executing tool update_draft"),
                               "update_reply_draft", "Error executing tool update_reply_draft: exact model detail")

    def test_wrong_reason_success_structured_data_and_multiple_blocks_refuse(self):
        module = self.module()
        text = "Error executing tool prepare_text_send"
        bad = [result("unrelated validation failure"), result(text, error=False),
               result(text, structured={"status": "prepared"}), result(text, count=2)]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(module.ProbeError):
                module.check_error(response, "prepare_text_send", text)
        with self.assertRaises(module.ProbeError):
            module.check_error(result(text), "unknown_tool", text)

    def test_service_and_pending_approval_state_guard_runs_after_masked_error(self):
        module = self.module()
        service = module.RefuseService()
        class Client:
            async def call_tool(self, *args, **kwargs):
                try: service.prepare_text_send(None)
                except AssertionError: pass
                return result("Error executing tool prepare_text_send")
        with self.assertRaisesRegex(module.ProbeError, "state_mutation"):
            asyncio.run(module.invoke(Client(), service, [], "prepare_text_send", {},
                                      "Error executing tool prepare_text_send"))
        self.assertEqual(service.calls, ["prepare_text_send"])

    def test_guard_runs_even_when_public_error_channel_is_wrong(self):
        module = self.module()
        service = module.RefuseService()
        class Client:
            async def call_tool(self, *args, **kwargs):
                service.pending["draft_a"] = ("changed", True)
                return result("wrong channel")
        with self.assertRaisesRegex(module.ProbeError, "state_mutation"):
            asyncio.run(module.invoke(Client(), service, [], "update_draft", {},
                                      "Error executing tool update_draft"))

    def test_forbidden_audit_effect_is_not_hidden_by_masked_error(self):
        module = self.module()
        service = module.RefuseService()
        effects = []
        class Client:
            async def call_tool(self, *args, **kwargs):
                effects.append("socket.connect")
                return result("Error executing tool update_draft")
        with self.assertRaisesRegex(module.ProbeError, "forbidden_effect"):
            asyncio.run(module.invoke(Client(), service, effects, "update_draft", {},
                                      "Error executing tool update_draft"))

    def test_pinned_library_executes_verified_bytes_without_loader_reread(self):
        module = self.module()
        self.assertTrue(hasattr(module, "load_library"), "verified library execution is missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "fixture.py"
            path.write_bytes(b'answer = "good"\n')
            path.chmod(0o444)
            s = path.stat()
            pin = {"path": str(path), "bytes": s.st_size,
                   "sha256": hashlib.sha256(b'answer = "good"\n').hexdigest(),
                   "mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "nlink": s.st_nlink}
            evil = compile('answer = "evil"', str(path), "exec")
            try:
                with patch.object(importlib.machinery.SourceFileLoader, "get_code", return_value=evil):
                    loaded = module.load_library("_sdk_probe_fixture", path, pin)
                self.assertEqual(loaded.answer, "good")
            finally:
                sys.modules.pop("_sdk_probe_fixture", None)

    def test_reply_metadata_prevalidation_selects_actual_request_model_detail(self):
        module = self.module()
        self.assertTrue(hasattr(module, "argument_error"), "actual metadata validation path is missing")
        from telegram_search_mcp.server import build_server
        from telegram_search_mcp.config import RuntimePolicy, KNOWN_CAPABILITIES
        from telegram_search_mcp.reply_drafts import PrepareReplyTextSendRequest
        from pydantic import ValidationError
        server = build_server(service_factory=module.RefuseService,
                              policy=RuntimePolicy(enabled_capabilities=KNOWN_CAPABILITIES))
        args = {"recipient": -1007, "text": "A🙂BC", "reply_to": {"chat_id": -1007, "message_id": 55},
                "outgoing_entities": False}
        wanted = [{"type": "list_type", "loc": ("outgoing_entities",), "msg": "Input should be a valid list"}]
        try:
            PrepareReplyTextSendRequest.model_validate(args)
        except ValidationError as error:
            expected = str(error)
        else:
            self.fail("invalid fixture admitted")
        detail = module.argument_error(server._tool_manager.get_tool("prepare_reply_text_send"), args, wanted)
        self.assertEqual(detail, expected)
        self.assertIn("PrepareReplyTextSendRequest", detail)


if __name__ == "__main__":
    unittest.main()
