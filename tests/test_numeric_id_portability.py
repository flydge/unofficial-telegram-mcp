"""Native ID boundaries must survive both Python and binary64 MCP consumers.

Staged test source only. Integrate and observe RED before applying the alias patch.
The public-input tests exercise MCP registration/validation; broker-output tests
replace only connection/frame I/O, retaining real handshake policy and decoders.
"""
from __future__ import annotations

import contextlib
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from pydantic import TypeAdapter, ValidationError

from telegram_search_mcp import schemas
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import contract_descriptor
from telegram_search_mcp.server import build_server


SAFE = 9007199254740991
UNSAFE_IDS = (9007199254740992, -9007199254740992,
              9223372036854775807, -9223372036854775808)


class NumericIdPortabilityTests(unittest.TestCase):
    def test_native_safe_endpoints_are_admitted_without_changing_integer_identity(self):
        for alias in (schemas.NumericTarget, schemas.ChatId, schemas.MessageId):
            adapter = TypeAdapter(alias)
            for value in (-SAFE, SAFE):
                with self.subTest(alias=alias, value=value):
                    actual = adapter.validate_python(value)
                    self.assertIs(type(actual), int)
                    self.assertEqual(actual, value)

    def test_ids_outside_the_native_safe_domain_are_rejected(self):
        for alias in (schemas.NumericTarget, schemas.ChatId, schemas.MessageId):
            adapter = TypeAdapter(alias)
            for value in UNSAFE_IDS:
                with self.subTest(alias=alias, value=value):
                    with self.assertRaises(ValidationError):
                        adapter.validate_python(value)

    def test_shared_ids_remain_strict_integers(self):
        for alias in (schemas.NumericTarget, schemas.ChatId, schemas.MessageId):
            adapter = TypeAdapter(alias)
            for value in (True, False, 1.0, 9007199254740991.0, "7", "-100123"):
                with self.subTest(alias=alias, value=value):
                    with self.assertRaises(ValidationError):
                        adapter.validate_python(value)

    def test_zero_and_username_policies_are_preserved(self):
        for alias in (schemas.NumericTarget, schemas.ChatId):
            with self.subTest(alias=alias):
                with self.assertRaises(ValidationError):
                    TypeAdapter(alias).validate_python(0)
        self.assertEqual(TypeAdapter(schemas.MessageId).validate_python(0), 0)
        self.assertEqual(TypeAdapter(schemas.MessageId).validate_python(-7), -7)
        request = schemas.SearchRequest(target="@synthetic", query={"text": "selected"})
        self.assertEqual(request.target, "@synthetic")

    def test_shared_request_and_response_fields_accept_safe_endpoints(self):
        for value in (-SAFE, SAFE):
            with self.subTest(value=value):
                search = schemas.SearchRequest(target=value, query={"text": "selected"})
                send = schemas.PrepareTextSendRequest(recipient=value, text="exact")
                attachment = schemas.AttachmentRequest(
                    anchor={"chat_id": value, "message_id": value})
                target = schemas.ResolvedTarget(chat_id=value, title="Synthetic",
                                                chat_type="supergroup")
                self.assertEqual(search.target, value)
                self.assertEqual(send.recipient, value)
                self.assertEqual(attachment.anchor.chat_id, value)
                self.assertEqual(attachment.anchor.message_id, value)
                self.assertEqual(target.chat_id, value)

    def test_oversized_nested_response_identities_are_rejected(self):
        for value in UNSAFE_IDS:
            for field in ("chat_id", "message_id"):
                anchor = {"chat_id": 7, "message_id": 8, field: value}
                with self.subTest(value=value, field=field):
                    with self.assertRaises(ValidationError):
                        schemas.AttachmentResponse(status="not_found", anchor=anchor,
                                                   detail="synthetic missing attachment")
            with self.subTest(value=value, field="resolved_target.chat_id"):
                with self.assertRaises(ValidationError):
                    schemas.ResolvedTarget(chat_id=value, title="Synthetic",
                                           chat_type="supergroup")

    def test_specialized_selected_message_ids_remain_positive(self):
        self.assertEqual(schemas.SelectedMessageAnchor(chat_id=7, message_id=SAFE).message_id,
                         SAFE)
        for value in (0, -1, -SAFE, *UNSAFE_IDS):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    schemas.SelectedMessageAnchor(chat_id=7, message_id=value)


class _BoundaryService:
    """External service substitute: no provider, disk, preparation or send effects."""
    def __init__(self):
        self.calls = []

    def search(self, request):
        self.calls.append(("search", request))
        return schemas.SearchResponse(status="no_match", matches=[],
            coverage=schemas.Coverage(complete=True, text_status="complete",
                metadata_status="not_requested", detail="synthetic complete search"))

    def prepare_text_send(self, request):
        self.calls.append(("prepare_text_send", request))
        return schemas.PrepareTextSendResponse(status="blocked", detail="synthetic blocked")

    def get_attachment(self, request):
        self.calls.append(("get_attachment", request))
        return schemas.AttachmentResponse(status="not_found", anchor=request.anchor,
                                           detail="synthetic missing attachment")

    def close(self):
        pass


class NumericIdPublicBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_public_requests_stop_before_service_or_provider_dispatch(self):
        service = _BoundaryService()
        factories = []

        def factory():
            factories.append(True)
            return service

        async with Client(build_server(service_factory=factory,
                                        policy=RuntimePolicy())) as consumer:
            for value in UNSAFE_IDS:
                cases = [
                    ("search_correspondence", {"target": value, "query": {"text": "selected"}}),
                    ("prepare_text_send", {"recipient": value, "text": "exact"}),
                    ("get_attachment", {"anchor": {"chat_id": value, "message_id": 8}}),
                    ("get_attachment", {"anchor": {"chat_id": 7, "message_id": value}}),
                ]
                for tool, arguments in cases:
                    with self.subTest(tool=tool, value=value):
                        result = await consumer.call_tool(tool, arguments)
                        self.assertTrue(result.is_error)
                        self.assertEqual(factories, [])
                        self.assertEqual(service.calls, [])
            # Controls rule out disabled/missing routes as the source of denial.
            for tool, arguments, status in [
                ("search_correspondence", {"target": -100123,
                    "query": {"text": "selected"}}, "no_match"),
                ("prepare_text_send", {"recipient": -100123, "text": "exact"}, "blocked"),
                ("get_attachment", {"anchor": {"chat_id": 7, "message_id": 8}}, "not_found"),
            ]:
                result = await consumer.call_tool(tool, arguments)
                self.assertFalse(result.is_error)
                data = result.structured_content
                if tool == "get_attachment":
                    self.assertEqual(set(data), {"result"})
                    data = data["result"]
                self.assertEqual(data["status"], status)
            self.assertEqual(len(factories), 1)
            self.assertEqual([name for name, request in service.calls],
                             ["search", "prepare_text_send", "get_attachment"])


class NumericIdBrokerOutputTests(unittest.TestCase):
    def test_oversized_broker_output_fails_closed_without_operation_retry(self):
        request = schemas.AttachmentRequest(anchor={"chat_id": 7, "message_id": 8})
        cases = [(None, None)] + [(field, value) for field in ("chat_id", "message_id")
                                 for value in UNSAFE_IDS]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                operations, connections, recovery = [], [], []
                anchor = {"chat_id": 7, "message_id": 8}
                if field is not None:
                    anchor[field] = value
                raw = {"status": "not_found", "anchor": anchor,
                       "detail": "synthetic missing attachment"}
                policy = RuntimePolicy()

                def connect(path):
                    connections.append(path)
                    return contextlib.nullcontext()

                def exchange(connection, operation, payload, **kwargs):
                    operations.append(operation)
                    if operation == "handshake":
                        return {**contract_descriptor(policy),
                                "broker_generation": "broker_" + "a" * 32}
                    if operation == "get_attachment":
                        self.assertEqual(payload, {"anchor": {"chat_id": 7, "message_id": 8}})
                        return raw
                    if operation == "release_client":
                        return {}
                    self.fail("unexpected broker operation: " + operation)

                proxy = BrokerClient(socket_path=Path("/unused-numeric-id.sock"),
                    connector=connect, policy=policy,
                    restart_callback=lambda: recovery.append("restart"),
                    sleeper=lambda seconds: recovery.append("sleep"))
                with patch.object(proxy, "_exchange", side_effect=exchange):
                    try:
                        result = proxy.get_attachment(request)
                        self.assertEqual(result.status, "not_found" if field is None else "error")
                        self.assertEqual(result.anchor.model_dump(),
                                         {"chat_id": 7, "message_id": 8})
                        self.assertEqual(operations, ["handshake", "get_attachment"])
                        self.assertEqual(len(connections), 1)
                        self.assertEqual(recovery, [])
                    finally:
                        # Cleanup is deliberately outside the counted operation assertion.
                        proxy.close()


if __name__ == "__main__":
    unittest.main()
