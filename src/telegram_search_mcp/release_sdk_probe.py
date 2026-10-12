"""Finite installed-SDK error characterization before full acceptance preparation.

Run this file under the intended installed interpreter with -I -S -B. The
explicit environment/request pins precede imports. The S11 span/revision error
contracts are retained; this short probe never substitutes for the full gate.
"""
import sys

if __name__ == "__main__" and (sys.flags.optimize or not (
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode)):
    raise SystemExit(78)

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

VERSION = 1
TOOLS = {"prepare_text_send", "prepare_reply_text_send", "update_draft", "update_reply_draft"}
ROUTES = ("plain", "topic", "reply", "topic_reply")


class ProbeError(ValueError):
    pass


class ErrorContractMismatch(ProbeError):
    def __init__(self, tool, actual, expected):
        super().__init__("wrong_error_reason_or_tool")
        self.detail = {"tool": tool, "actual": actual[:4096], "expected": expected[:4096]}


def need(value, reason):
    if not value:
        raise ProbeError(reason)


def check_error(result, tool, expected_text):
    need(tool in TOOLS, "unknown_tool")
    need(type(expected_text) is str and expected_text.startswith("Error executing tool " + tool)
         and len(expected_text.encode()) <= 16384, "expected_error_contract")
    need(result.is_error is True and result.structured_content is None, "wrong_error_channel")
    need(type(result.content) is list and len(result.content) == 1
         and result.content[0].type == "text", "wrong_error_reason_or_tool")
    if result.content[0].text != expected_text:
        raise ErrorContractMismatch(tool, result.content[0].text, expected_text)


class RefuseService:
    def __init__(self):
        self.calls = []
        self.provider_calls = []
        self.hydration_calls = []
        self.owner_calls = []
        self.claims = []
        self.pending = {"draft_" + c * 32: (route, False) for c, route in zip("abcd", ROUTES)}
        self.close_calls = 0

    def snapshot(self):
        return copy.deepcopy((self.calls, self.provider_calls, self.hydration_calls,
                              self.owner_calls, self.claims, self.pending))

    def _refuse(self, name):
        self.calls.append(name)
        raise AssertionError("unexpected service method")

    def prepare_text_send(self, request): self._refuse("prepare_text_send")
    def prepare_reply_text_send(self, request): self._refuse("prepare_reply_text_send")
    def update_draft(self, request): self._refuse("update_draft")
    def update_reply_draft(self, request): self._refuse("update_reply_draft")
    def close(self): self.close_calls += 1


async def invoke(client, service, forbidden, tool, args, expected_text):
    before = service.snapshot()
    try:
        value = await client.call_tool(tool, args, read_timeout_seconds=15)
        check_error(value, tool, expected_text)
        return value
    finally:
        # These assertions run even for the exact expected masked body exception.
        need(service.snapshot() == before, "state_mutation")
        need(not forbidden, "forbidden_effect")


def argument_error(tool, arguments, wanted):
    from pydantic import ValidationError
    try:
        # Includes reply pre_parse_json request validation, not just arg_model.
        tool.fn_metadata.validate_arguments(arguments)
    except ValidationError as error:
        need(error.errors(include_url=False, include_context=False, include_input=False) == wanted,
             "argument_model_error_reason")
        return str(error)
    raise ProbeError("argument_model_admitted_invalid_request")


def load_library(name, file, row, *, package=False):
    """Compile exactly the hash-checked bytes instead of rereading via a loader."""
    need(set(row) == {"path", "bytes", "sha256", "mode", "uid", "nlink"}
         and row["path"] == str(file)
         and all(type(row[k]) is int for k in ("bytes", "mode", "uid", "nlink")), "library_binding")
    before = file.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_size == row["bytes"]
         and stat.S_IMODE(before.st_mode) == row["mode"]
         and before.st_uid == row["uid"] and before.st_nlink == row["nlink"] == 1, "library_drift")
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        need(os.fstat(stream.fileno()) == before, "library_drift")
        body = stream.read(1024 * 1024 + 1)
        need(os.fstat(stream.fileno()) == before, "library_drift")
    need(len(body) <= 1024 * 1024 and file.lstat() == before
         and hashlib.sha256(body).hexdigest() == row["sha256"], "library_drift")
    spec = importlib.util.spec_from_file_location(name, file,
        submodule_search_locations=[str(file.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(body, str(file), "exec"), module.__dict__)
    return module


def _bootstrap(path, expected):
    """Small trust bootstrap: pinned sibling tooling, never caller-selected code."""
    s = path.lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and not stat.S_IMODE(s.st_mode) & 0o222
         and s.st_size <= 8 * 1024 * 1024, "request_metadata")
    raw = path.read_bytes()
    need(path.lstat() == s and hashlib.sha256(raw).hexdigest() == expected, "request_drift")
    def unique(pairs):
        value = {}
        for k, v in pairs:
            need(k not in value, "duplicate_key")
            value[k] = v
        return value
    request = json.loads(raw, object_pairs_hook=unique)
    need(type(request) is dict and set(request) == {"schema", "runtime", "libraries",
         "implementation_pins", "environment_manifest", "expected_tools", "source_version"}
         and request["schema"] == "tgcmp-sdk-error-probe-request/v1", "request_schema")
    runtime = request["runtime"]
    need(runtime["executable"] == os.path.abspath(sys.executable)
         and runtime["prefix"] == os.path.abspath(sys.prefix), "runtime_identity")
    site = Path(runtime["site"])
    need(site.is_absolute() and site.resolve() == site and str(site) == runtime["site"]
         and site.is_relative_to(Path(sys.prefix)), "site_binding")
    siblings = Path(__file__).resolve().parent
    need(set(request["libraries"]) == {"release_evidence", "release_environment"}, "library_binding")
    loaded = {}
    # Installed package parent remains installed: tooling is the only explicit
    # sibling override. Other telegram_search_mcp imports resolve in pinned SITE.
    sys.path.insert(0, str(site))
    package_path = site / "telegram_search_mcp/__init__.py"
    package_pins = [row for row in request["implementation_pins"] if row["path"] == str(package_path)]
    need(len(package_pins) == 1, "installed_package_binding")
    telegram_search_mcp = load_library("telegram_search_mcp", package_path, package_pins[0], package=True)
    need(Path(telegram_search_mcp.__file__).resolve().is_relative_to(site)
         and telegram_search_mcp.__version__ == request["source_version"], "installed_package_origin")
    for name in ("release_evidence", "release_environment"):
        row = request["libraries"][name]
        file = siblings / (name + ".py")
        need(row["path"] == str(file), "library_binding")
        qualified = "telegram_search_mcp." + name
        module = load_library(qualified, file, row)
        loaded[name] = module
    ev = loaded["release_evidence"]
    request_pin = ev.current_pin(path)
    need(request_pin["sha256"] == expected, "request_drift")
    need(ev.same_json(ev.load_json(request_pin, immutable=True), request), "request_drift")
    ev.verify_pins(list(request["libraries"].values()) + request["implementation_pins"])
    environment = loaded["release_environment"]
    observation = environment.check_environment(request["environment_manifest"], stage="sdk", moment="before")
    baseline = ev.load_json(request["environment_manifest"], immutable=True)
    need(ev.same_json(baseline["runtime"], runtime) and runtime["packages"].get("mcp") == "2.2.0",
         "unsupported_sdk_runtime")
    return request, ev, environment, observation, baseline


async def run_probe(request, forbidden):
    from mcp import Client
    from pydantic import ValidationError
    from telegram_search_mcp.server import build_server
    from telegram_search_mcp.config import RuntimePolicy, KNOWN_CAPABILITIES
    from telegram_search_mcp.schemas import PrepareTextSendRequest
    from telegram_search_mcp.draft_models import UpdateDraftRequest
    from telegram_search_mcp.reply_drafts import PrepareReplyTextSendRequest, UpdateReplyDraftRequest
    models = {"prepare_text_send": PrepareTextSendRequest, "prepare_reply_text_send": PrepareReplyTextSendRequest,
              "update_draft": UpdateDraftRequest, "update_reply_draft": UpdateReplyDraftRequest}
    service = RefuseService()
    factories = []
    def factory():
        factories.append("created")
        return service
    server = build_server(service_factory=factory, policy=RuntimePolicy(enabled_capabilities=KNOWN_CAPABILITIES))
    results = []
    async with Client(server) as client:
        tools = await client.list_tools()
        need(len(tools.tools) == request["expected_tools"] == 43, "schema_census")
        for char, route in zip("abcd", ROUTES):
            reply = route in ("reply", "topic_reply")
            topic = route in ("topic", "topic_reply")
            tool = "prepare_reply_text_send" if reply else "prepare_text_send"
            args = {"recipient": -1007, "text": "A🙂BC"}
            if reply: args["reply_to"] = {"chat_id": -1007, "message_id": 55}
            if topic: args["topic"] = {"kind": "forum", "id": 41}
            for family in ("structural", "semantic", "revision"):
                target = tool
                if family == "structural":
                    invalid = {**args, "outgoing_entities": False}
                    want = [{"type": "list_type", "loc": ("outgoing_entities",), "msg": "Input should be a valid list"}]
                elif family == "semantic":
                    invalid = {**args, "outgoing_entities": [{"offset": 1, "length": 1,
                                                         "type": {"@type": "textEntityTypeBold"}}]}
                    want = [{"type": "value_error", "loc": (),
                             "msg": "Value error, Invalid whole-scalar UTF-16 range"}]
                else:
                    target = "update_reply_draft" if reply else "update_draft"
                    invalid = {"draft_id": "draft_" + char * 32, "outgoing_entities": []}
                    want = ([{"type": "missing", "loc": ("text",), "msg": "Field required"}] if reply else
                            [{"type": "value_error", "loc": (),
                              "msg": "Value error, exactly one of text or caption is required"}])
                try:
                    models[target].model_validate(invalid)
                except ValidationError as error:
                    detail = str(error)
                    need(error.errors(include_url=False, include_context=False, include_input=False) == want,
                         "model_error_reason")
                else:
                    raise ProbeError("model_admitted_invalid_request")
                channel = "masked" if target in {"prepare_text_send", "update_draft"} and family != "structural" else "detailed"
                if family == "structural":
                    detail = argument_error(server._tool_manager.get_tool(target), invalid, want)
                text = "Error executing tool " + target + (": " + detail if channel == "detailed" else "")
                await invoke(client, service, forbidden, target, invalid, text)
                results.append({"id": route + "/" + family, "tool": target, "channel": channel,
                                "model_errors": want, "diagnostic_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                "state_unchanged": True})
    need(len(results) == 12 and not service.calls and not forbidden, "probe_census")
    return {"schema": "tgcmp-installed-sdk-error-probe/v1", "tool_version": VERSION,
            "status": "SDK_ERROR_PREFLIGHT_PASSED", "acceptance": False, "sdk_calls": 12,
            "routes": list(ROUTES), "cases": results, "service_calls": service.calls,
            "provider_calls": service.provider_calls, "hydration_calls": service.hydration_calls,
            "owner_calls": service.owner_calls, "claims": service.claims,
            "pending_and_approval_unchanged": True, "forbidden_effects": forbidden,
            "lazy_factory_calls": len(factories), "service_close_calls": service.close_calls,
            "limitation": "In-process installed SDK with synthetic service; full installed SDK/CLI/client gates remain required."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    phase = "bootstrap"
    forbidden = []
    try:
        request, ev, env, before, baseline = _bootstrap(args.request, args.request_sha256)
        allowed = {row["path"] for rows in baseline["files"].values() for row in rows}
        allowed.update(row["path"] for row in request["libraries"].values())
        allowed.update((str(args.request), request["environment_manifest"]["path"],
                        baseline["spec"]["path"], str(Path(__file__).resolve())))
        allowed.update(baseline.get("absent_paths", []))
        def guard(event, values):
            read_path = None
            denied = event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork",
                "socket.connect", "socket.bind", "socket.getaddrinfo", "os.mkdir", "os.remove", "os.rmdir",
                "os.rename", "os.link", "os.symlink", "os.chmod", "os.chown", "os.truncate"}
            if event == "open" and not isinstance(values[0], int):
                mode, flags = values[1], values[2]
                denied = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
                if not denied:
                    path = os.path.realpath(os.path.abspath(os.fspath(values[0])))
                    read_path = path
                    denied = path not in allowed
            if denied:
                forbidden.append({"event": event, **({"read_path": read_path} if read_path else {})})
                raise ProbeError("forbidden_effect")
        sys.addaudithook(guard)
        phase = "sdk_routes"
        value = asyncio.run(asyncio.wait_for(run_probe(request, forbidden), timeout=60))
        value["environment_before"] = before
        phase = "environment_after"
        value["environment_after"] = env.check_environment(request["environment_manifest"], stage="sdk", moment="after")
        value["request"] = ev.current_pin(args.request)
        value["entrypoint"] = ev.current_pin(Path(__file__).resolve())
        phase = "origins"
        origins = []
        for name, module in tuple(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if origin:
                path = str(Path(origin).resolve())
                need(path in allowed, "loaded_origin")
                origins.append({"module": name, "path": path})
        value["loaded_origins"] = origins
        need(not forbidden, "forbidden_effect")
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except BaseException as error:
        mismatches = []
        def reasons(value):
            if isinstance(value, BaseExceptionGroup):
                return [r for child in value.exceptions for r in reasons(child)]
            if isinstance(value, ErrorContractMismatch):
                mismatches.append(value.detail)
            return [str(value) if isinstance(value, (ProbeError, ValueError)) else type(value).__name__]
        print(json.dumps({"status": "SDK_ERROR_PREFLIGHT_REFUSED", "acceptance": False,
                          "phase": phase, "reasons": reasons(error)[:4],
                          "attempted_effects": forbidden[:4], "error_mismatches": mismatches[:1]}))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
