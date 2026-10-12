# Release preparation tools

These versioned tools are preparation diagnostics. Every output has
`acceptance: false`. They neither run nor replace fullsuite, build/install,
installed SDK/CLI, clients, native consumer/provider or independent closure
gates. Product acceptance still requires fresh gate execution, raw scenario and
call census, terminal receipts, owned child waits and current before/after pins.

## Explicit inputs and environment

Use the intended verification interpreter with bytecode writes disabled. Create
an immutable JSON input with its expected SHA-256 recorded before invocation.
File pins contain canonical path, size, SHA-256, mode, uid and exact nlink.
Source, harness, protected files and evidence require nlink1. Dependencies alone
may declare a larger positive count; any count drift still refuses. All file
metadata is checked before bulk hashing. Large files are streamed.

`release_environment` captures an already pinned
`tgcmp-chain-environment-spec/v1` into a fresh output directory. The spec names
the executable spelling, prefix, site, Python/package versions, source/harness/
dependency/protected pins, native aliases, interpreter symlink, explicit links,
closed dependency trees and expected absent paths. Tree traversal errors refuse;
unreadable directories cannot disappear from the census. Configuration contents
are never output. Expected absent import cache/entry-point paths are finite,
explicit and checked before and after; they do not authorize existing unpinned
files. Capture cannot overwrite an existing baseline or silently adopt drift.

```
python -B -m telegram_search_mcp.release_environment capture \
  --input SPEC --input-sha256 EXPECTED_SHA --output FRESH_DIRECTORY
python -B -m telegram_search_mcp.release_environment check \
  --input MANIFEST --input-sha256 EXPECTED_SHA --stage sdk --moment before
```

Use the same manifest for `fullsuite`, `package`, `sdk`, `clients`, with both
`before` and `after` observations. The manifest binds the verification observer
and explicitly named source; it does not relocate to a newly built venv or
substitute for each gate's own interpreter, startup and loaded-origin checks.
An interpreter/source/harness change requires a separately explicit pinned spec
and fresh namespace. Unexpected third-party or protected drift requires the
applicable authorization; do not regenerate pins automatically.

## Installed SDK error probe

Run the tracked `release_sdk_probe.py` file using the exact installed interpreter
with `-I -S -B`. Its pinned `tgcmp-sdk-error-probe-request/v1` names two sibling
tooling libraries, installed implementation pins, the shared environment,
source version and 43 expected tools. The installed package parent is loaded
from verified bytes; product imports remain in installed SITE. Only the two
named tooling modules are overridden. Optimized/nonisolated startup refuses78.

```
INSTALLED_PYTHON -I -S -B /absolute/source/release_sdk_probe.py \
  --request REQUEST --request-sha256 EXPECTED_SHA
```

The finite probe checks plain/topic/reply/topic_reply routes for structural,
whole-scalar UTF-16 and entity-only revision refusals: 12 SDK calls. Structural
diagnostics follow the installed metadata's `validate_arguments`, including
reply request prevalidation. Body refusals preserve exact masked plain errors
and detailed reply errors. Wrong tool, reason, channel or success refuses.
Pending/approval, service/provider/hydration/owner/claim and forbidden-effect
assertions run even after masked errors. Process/network/write operations are
denied; file reads require explicit pins or declared absent paths. Loaded module
origins and both environment observations are retained.

This uses a synthetic service, not live Telegram. Lazy factory and close counts
are reported separately from authority calls. The server registers lazy-service
close at process exit; the JSON counter is sampled before that exit callback.
No actual provider resource is opened by this probe. Full lifecycle, CLI, expiry,
capability-revocation, payload-immutability and lost-response/no-retry gates remain.

## Shared historical readers and real preparation adoption

`release_evidence` tool version1 explicitly supports F12 fullsuite and P06 package
independent-review layouts. It checks expected input hashes before deriving any
output bindings; unknown schemas, missing witnesses, duplicate keys/pins, wrong
types and contradictory statuses refuse. It retains original reports, wrapper/
list inventories, literal corrective `+=` rows, schema/scenario/call counts,
terminal receipts and process closure. JSON and tooling execute/parse only the
verified bytes. AST inventory does not execute source or accept alias/index
mutation. Output is historical DATA, never fresh product PASS.

```
python -B -m telegram_search_mcp.release_evidence \
  --request REQUEST --request-sha256 EXPECTED_SHA
python -B -m telegram_search_mcp.release_preflight V2_MANIFEST --sha256 EXPECTED_SHA
```

The existing standalone `release_preflight.py V1_MANIFEST` still works. Pinned
v2 runs in module context, checks the shared environment first, then existing
runtime/source/harness/socket guards. SDK stage requires a typed, current probe
receipt bound to the same environment, entrypoint, request, libraries and
implementation. Optional `evidence_request` invokes the shared reader/assembler
only in `before`; other stages require a null SDK receipt. Output provenance
includes tool versions, entrypoint/library pins and request pins. All namespaces
are explicit and fresh; old failed/accepted receipts are preserved.

The 12 October maintenance demonstration intentionally binds frozen 0.51.0
source and two accepted historical F12/P06 reports alongside the new preparation
harness. This validates reuse, not a new release of that harness. Before the next
existing acceptance chain, name that chain's exact source/interpreter, run this
preflight early and retain every unchanged full quality gate and independent
closure review. New report layouts need an explicit adapter and refusal tests;
never reinterpret historical DATA as new acceptance.
