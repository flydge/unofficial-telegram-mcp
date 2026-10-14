---
name: telegram-setup
description: Install the authenticated local Unofficial Telegram MCP bundle and guide native Terminal setup, resume, and status without collecting credentials.
---

# Telegram setup

This skill is paired with unreleased candidate `0.52.0.dev0`, contract 1.
The private bundle requires Apple Silicon macOS 26+, native Apple Terminal,
Codex, and the owner's Telegram account/API application. The admitted bundle
supplies Python, dependencies and the pinned native runtime. A public download
command and clean-user native acceptance are not established.

Use only an independently authenticated bundle index and a verifier seed whose
digest and byte size come from the reviewed trust input. Keep installer failures
and recoverable transaction evidence. Refuse foreign runtime, registration,
service or skill conflicts; never stop an existing owner to make install pass.

After installation, read the nonsecret `activation_arguments` returned by the
installer. Ask the owner to run that exact command ending in `setup` from a
foreground, uncaptured Apple Terminal. The same installed launcher accepts
`resume` for an interrupted managed enrollment and `status` for bounded,
read-only enrollment/handshake observations. Do not run authentication through
captured agent execution. Do not collect, echo, store or transmit API ID/hash,
phone, Telegram login code or optional 2FA; the native prompts hide input.
Never put secrets in command arguments, environment variables or stdin pipes.

The preserved default session is `~/Library/Application Support/TelegramSearchMCP/tdlib`.
The separate runtime root owns only this release, installation receipts and
its matched Codex registration/paired skills. There is no account alias or
profile registry, purge, maintenance, cloud auth, or model provisioning route.

Installation ends as `activation_required`. Status or launch success is not
Ready. Native setup must finish supported auth, confirm provider Closed and native
destruction, release the shared owner, and hand off to the exact receipt-bound
broker. The broker must pass matching package/contract/config/schema and fresh
Ready checks. Then reconnect Codex and use the paired telegram-search skill's
manifest gate and one authorized bounded own-chat search. Only that real
consumer evidence can establish onboarding acceptance. Unknown teardown or an
unsupported auth state remains refused; preserve the owner and use the stated
resume action instead of retrying another transport.

Source/redistribution completeness and public hosting/publisher trust remain
separate open gates. Do not call this candidate released or download missing
source, runtime, optional media tools or models without applicable authority.
