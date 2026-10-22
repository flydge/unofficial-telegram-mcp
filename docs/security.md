# Unofficial Telegram MCP security model

## Unreleased default-session enrollment

The development candidate's local `telegram-search-setup` path is technical
support beyond published 0.51.0. It does not change MCP schemas, data-request
allowlists or legacy capability defaults. Its separate typed auth transport has
no MCP/broker/raw-dispatch interface. Setup/resume require a foreground native
Apple Terminal with bounded ancestry/TTY checks and hidden input; help/status
never read Keychain, initialize a native client or start a service. These checks
refuse known captured/agent/remote intermediaries; they do not claim protection
against arbitrary hostile software running as the same OS user or a separately
compromised terminal.

Broker and setup share one private stable-inode nonblocking `broker.lock` lease.
Contention returns `owner_busy` without killing the owner, changing its socket or
resetting a session. Setup holds the lease through auth close and native destroy,
including cancellation/error cleanup. Only after teardown and release can the
matching installed broker start. A new plist is published without replacing an
existing name; existing plist/live registration must match and remain unchanged.
Before accepting a competing Ready owner, read-only validation must prove the
same intended installed definition/live registration. Matching IPC contract/config
and fresh Ready are also required; a compatible socket alone is insufficient.

Observed provider Closed and completed native destruction are independent facts.
If destroy fails or is interrupted, the process retains the exact lease fd and
auth owner through context exit, garbage collection and later setup calls until
OS process exit. It never automatically retries a possibly completed destroy or
starts a broker. `auth_destroy_failed` requires the owning process to exit before
resume. A provider-close error with confirmed native destruction can release
ownership but cannot report Ready.

Enrollment is confined to a genuinely new default session. A bounded private
atomic durable root record plus a private durable session identity marker bind
the exact path, directory inodes, UID, enrollment identity and dedicated Keychain
database-key account. API records are preserved and only absent accounts are
added through native Security.framework; denied/locked/unreadable differs from
absent. A new random 32-byte key is stored before the pending record, and the
record/marker precede TDLib parameters. Interrupted initial persistence can leave
an unused key or incomplete metadata; it never opens a database and requires
honest recovery instead of silent adoption or key regeneration. Missing/damaged
or inconsistent bindings/keys refuse before managed native initialization.

Phone/code/optional password are used only by the local auth process; uncertain
teardown can retain its auth owner until that process exits. Errors, status and
representations contain bounded codes, not provider
messages or input. Python immutable strings/bytes are not claimed to be zeroized.
TDLib JSON byte fields use base64 for both parameters and the resumed encryption
key branch. Unmanaged legacy sessions keep their empty-key behavior; no existing
database is rekeyed. Older binaries cannot open a newly encrypted managed session,
so retain its exact key and metadata when rolling back package code.

Provider state is reread on resume; local pending/ready checkpoints never prove
current authorization. Failed or uncertain credential submissions are not
replayed automatically. Resume before enrollment uses the same new-session
admission and can recover partial API writes or early credential cancellation;
existing credentials are never overwritten and foreign/damaged state refuses.
Cancellation preserves state and attempts teardown without logout/revoke/deletion,
retaining ownership until process exit when native destruction is uncertain.
In this Terminal flow, QR/email/registration/purchase
states are unsupported (QR is handled by `connect`, see the QR connect boundary below) and return
redacted actionable status. New policy enables only `read` and
`artifacts`; an existing policy remains byte-identical. Synthetic tests do not
close native Keychain/Telegram/launchd or clean-user acceptance, pinned runtime
provisioning, one-command installation or MCP/skill registration gates.

## Authority boundary

The dedicated TDLib session holds the Telegram account's authority. Exactly one launchd-managed broker owns that session and the sole `TDLibClient`; per-task STDIO MCP proxies never load TDLib. Unofficial Telegram MCP narrows authority at three layers: MCP exposes the fifty-five tools of the current candidate listed in the [tool reference](reference.md#mcp-surface) (published 0.51.0: forty-three); the private broker protocol accepts only the matching fixed operations plus `check`, `health`, and `release_client`; and the TDLib client rejects request types outside its fixed allowlist before transport:

```text
getAuthorizationState
setTdlibParameters
checkDatabaseEncryptionKey
getMe
createPrivateChat
searchPublicChat
searchChats
searchChatsOnServer
getChats
loadChats
getChat
getChatListsToAddChat (closed exact-chat read of the Main/Archive lists Telegram offers for one chat)
getSupergroup
getForumTopics
getForumTopic
getForumTopicHistory
searchMessages
searchChatMessages
getMessage
getMessageProperties (closed exact-chat reply and forward/copy eligibility read)
getChatHistory
getMessageLink
getUser
downloadFile
getFile
cancelDownloadFile
sendMessage (fixed text, document, photo, voice-note, or native-video shape, only after explicit draft approval)
forwardMessages (closed single-message forward/copy shape, only after explicit draft approval)
addChatToList (closed Main or Archive shape for one chat, only after explicit draft approval)
editMessageText (closed plain-text shape for one own text message, only after explicit draft approval)
```

`createPrivateChat` has one narrowly approved use: after `getMe` identifies the authorized account, Saved Messages resolution calls it with that same user ID and verifies the returned private chat. `loadChats` advances only a fixed Main or Archive catalog lane (limit 50); its one other use is a single limit-100 load of each of the two lists when an archive read meets a chat that a fresh session has not loaded yet (see the F17a boundary). `searchMessages` is reachable only through the typed discovery wrapper and the exact request shape below. None of these methods is a general bridge to caller-chosen TDLib operations.

`forwardMessages` has one closed shape, validated before transport and built by the broker from an approved draft (source chat, message ID, recipient chat and mode); callers supply no other field: `chat_id` (the recipient), `topic_id` exactly `null`, `from_chat_id`, `message_ids` of length exactly one, `options` exactly `{"@type":"messageSendOptions","suggested_post_info":null,"disable_notification":false,"from_background":false,"protect_content":false,"allow_paid_broadcast":false,"paid_message_star_count":0,"update_order_of_installed_sticker_sets":false,"scheduling_state":null,"effect_id":0,"sending_id":<random int32, non-zero>,"only_preview":false}`, `send_copy` (boolean) and `remove_caption` (boolean, false unless `send_copy`). Any other key, type or value (several messages, a topic, scheduling, paid or silent options) raises a forbidden-request error before the request reaches TDLib. See [Exact forward/copy boundary](#exact-forwardcopy-boundary-f14-candidate).

`inputMessageVideo` is the only content constructor added to `sendMessage` for native video, and it too is built by the broker from an approved draft. Besides the text, document, photo and voice-note shapes, the request must be exactly: `chat_id` (the non-zero recipient), `topic_id`, `reply_to` and `reply_markup` all `null`, `options` exactly the fixed `messageSendOptions` listed above for `forwardMessages` (including a random non-zero `sending_id`), and `input_message_content` exactly `{"@type":"inputMessageVideo","video":{"@type":"inputVideo","video":{"@type":"inputFileLocal","path":<staged path>},"thumbnail":null,"cover":null,"start_timestamp":0,"added_sticker_file_ids":[],"duration":<1..10800>,"width":<16..8192>,"height":<16..8192>,"supports_streaming":<boolean>},"caption":{"@type":"formattedText","text":<at most 1024 characters>,"entities":[]},"show_caption_above_media":false,"self_destruct_type":null,"has_spoiler":false}`, with the longer side at most 20 times the shorter. The file must be the broker's own staged copy: a path whose parent is a `draft_`-prefixed directory directly under the staging root. Any other key, type or value (a thumbnail or cover, a start timestamp, stickers, caption entities, a spoiler, a self-destruct timer, a reply, a topic, a remote or unstaged file) raises a forbidden-request error before the request reaches TDLib. See [Native video boundary](#native-video-boundary-f15a-candidate).

`addChatToList` has one closed shape, validated before transport and built by the broker from an approved draft; callers supply nothing but the draft: `chat_id` (a non-zero integer within 2^53-1 in magnitude) and `chat_list` exactly `{"@type":"chatListMain"}` or `{"@type":"chatListArchive"}`, with no other key. A folder list (`chatListFolder`), a null or string list, an extra key or a boolean or zero chat ID raises a forbidden-request error before the request reaches TDLib.

`editMessageText` likewise has one closed shape built from an approved draft: `chat_id` (non-zero), `message_id` (1 to 2^53-1), `reply_markup` exactly `null` and `input_message_content` exactly `{"@type":"inputMessageText","text":{"@type":"formattedText","text":<1 to 4096 characters>,"entities":[]},"link_preview_options":null,"clear_draft":false}`. The text must already be in the shared normalized form (NFKC, line feed only, no control or format characters other than line feed, and Telegram's rewrite already applied): no edge whitespace and none of tab, U+2028 to U+202E, U+030A, U+0333 and U+033F. Any other key, type or value (entities or styles, a reply markup, link-preview options, draft clearing, another content constructor, unnormalized or edge-whitespace text) raises a forbidden-request error before the request reaches TDLib.

`getChatListsToAddChat` is a read with one closed shape: `chat_id` (a non-zero integer within 2^53-1 in magnitude) and no other key; any extra key or a boolean, zero, string or out-of-range chat ID raises a forbidden-request error before the request reaches TDLib. `loadChats` admits exactly the limits 50 (catalog lane) and 100 (the one-shot load above) for `chatListMain` or `chatListArchive`.

Both request types travel only through one dedicated one-attempt change transport. The generic provider call refuses them even in their exact closed shape, and the change transport refuses every other request type, re-validates the shape, runs the capability and claim guard immediately before the raw send and never retries. See [Archive and own text edit boundary](#archive-and-own-text-edit-boundary-f17a-candidate).

The broker socket lives in an owner-only directory with mode `0700`; the socket and owner lock use mode `0600`. The broker also verifies the kernel-reported peer UID through Darwin `getpeereid`. A nonblocking owner lock prevents a second broker. Only the lock holder may replace a same-owner, non-symlink Unix socket after proving that it is stale. Same-UID processes are inside this local filesystem trust boundary. IPC version 2 requires a same-connection contract handshake before every operation; IPC version 1 has no execution compatibility. The handshake compares loaded package/contract/finalized schema identity and effective trusted config. The next request binds the process generation; config freshness and capability checks occur before dispatch. Handshake-only diagnostics do not initialize TDLib. Requests use versioned length-prefixed strict JSON, a fixed operation allowlist, public-model validation on both sides, 1 MiB request and 8 MiB response limits, random process-lifetime client IDs, random request IDs, a 250-millisecond per-read bound, and monotonic deadlines capped at 570 seconds.

The broker fails closed unless the dedicated session reaches `authorizationStateReady`. It never accepts API credentials, login codes, passwords, session paths, index paths, account selectors, wildcards, caller-controlled provider limits/filters/dates/functions, or arbitrary TDLib requests through MCP or IPC. `api_id` and `api_hash` are read by the broker only from the fixed macOS Keychain service or, for a session enrolled with the bundled application identity, from the hash-authenticated `app-identity.json` in the installed release; they are never logged or included in an error.

## Artifact and upload ownership in the development candidate

Artifact IDs and upload tokens preserve their public formats, but broker dispatch
now captures a client ID, the fixed broker generation and a private lifecycle
identity. Central store redemption checks that memory-only grant before reading
bytes or exposing metadata. Downloaded, created, upload-finished and derived
artifacts inherit this authority, including page images, media frames and voice
conversions. Outgoing draft admission and internal re-lookups use the same check.
Foreign, expired and unbound IDs return content-free unavailable responses.

A normal socket reconnect with the same process client ID retains its grants.
Explicit client release revokes local-only clients too; reusing that ID creates a
new lifecycle and cannot revive old artifacts, uploads or artifact drafts. Release
and publication are serialized, while long download, parse, conversion and copy
work is revalidated before publishing output. After a claim, potentially long staging is followed by a fresh account/lifecycle
check. A final pure lifecycle/epoch fence serializes the irreversible TDLib raw
send with release; account observations and provider confirmation waits happen
outside that fence and outside broker-state/draft-registry critical sections.
Release or observed drift before this boundary prevents the unissued send and
retains a local-failure record for the consumed claim. Once raw dispatch crossed
that boundary, release cannot cancel the in-flight attempt. Its captured original
claim/provider identity retains terminal or unknown evidence privately, while
revoked clients cannot disclose or replay it and no automatic retry is allowed.
Observed auth loss through draft operations uses the same revoking account
boundary as provider-bound artifact reads; same-account recovery cannot restore
those grants. Offline local grants remain usable. The artifact's original 12-hour and
upload's 15-minute expiry remain fixed; reader context idle expiry does not shorten
these lifetimes. Broker restart denies previously persisted unbound bytes without
adopting or deleting those cache files.

The call that made the attempt never reports `expired` for it. When artifact
authority, scope or account checks fail after the claim (the post-send owner check,
the publication account check, or an unavailable artifact reaching dispatch),
`send_prepared_artifact` and `send_prepared_video` consult the draft registry by
draft ID, calling client and send-tool kind. An unclaimed draft is `expired` as
before; a claim that ended in a local failure returns that content-free result
(`failed` with "no provider send was attempted" for artifacts, `local_failed` for
video); any other claim returns `outcome_unknown` with "current receipt authority is
unavailable; do not retry automatically". The lookup is routing only: it discloses
no provider evidence (status, message ID), authorizes no status read or second send,
and lasts at most the 900-second attempt retention. Another client, or the other send
tool, still sees `expired`. A later call that no longer holds the original client
lifecycle and account epoch keeps the existing content-free `expired` replay refusal
and never resends. This closes a misleading `expired` that previously invited a
duplicate artifact send; it does not extend to later calls (see the residual risks in
[Native video boundary](#native-video-boundary-f15a-candidate)).

Offline create, upload and local readers use the trusted fixed session namespace
and do not initialize TDLib solely to obtain a numeric account ID. Provider-bound
work binds separately observed numeric identity and rechecks it before publication.
Observed account drift or authorization loss revokes provider-bound authority;
switching back cannot revive it. This is one supported default-session namespace,
not multi-account routing or detection of an unsupported external session
replacement before any provider observation. Same-UID client impersonation remains
outside this IPC isolation boundary. The underlying byte store's unscoped Python
API is internal; MCP cannot import or adopt an unbound persisted artifact.

## Explicit outgoing text styles

The four text preparation/revision tools accept optional `outgoing_entities`.
Enable the separate default-off `formatted_text_send` capability together with
`send`, the selected topic/reply capability and every required reply-source
capability. Incoming source styles never enable or populate outgoing styles.

Pass an actual list of at most 32 closed records, for example
`[{"offset":0,"length":5,"type":{"@type":"textEntityTypeBold"}}]` for `Hello`.
The five fieldless types are `textEntityTypeBold`, `textEntityTypeItalic`,
`textEntityTypeUnderline`, `textEntityTypeStrikethrough` and `textEntityTypeSpoiler`.
Offsets and lengths are strict integers in UTF-16 code units of the **complete
final normalized text** (NFKC, CRLF/CR converted to LF, then Telegram's rewrite: a tab becomes a space; U+2028, U+2029, U+030A, U+0333 and U+033F are removed; leading and trailing spaces and line feeds are trimmed), on whole Unicode scalar
boundaries. Normalize and recalculate spans before preparation; no parser or
automatic span rebasing is provided. Null, extra fields, duplicate/crossing spans
and overlapping or adjacent spans of the same style are rejected. Distinct styles
may nest or cover the same range. Hidden links, mentions, custom emoji, quotes,
code and formatted attachment captions are outside this outgoing subset.

Review the full text, every style/range/covered substring including spoilers,
account, recipient, optional exact topic and complete reply evidence, expiry and
one approval digest. Ordinary/topic formatted previews use v2; formatted replies
use one outer v3 digest with no inner digest. Empty lists preserve existing plain
preview and wire behavior. A text update replaces the complete text and style
list; omitted or empty lists clear styles. Entity-only updates are rejected.
Ordinary refresh preserves styles; topic refresh requires fresh preparation.
Every revision has a new draft ID and requires fresh approval.

The same prepared provider/session and the full capability union must remain
current through preview, revision, owner confirmation, claim, native registration,
final transport, status and receipt replay. Provider evidence must match the exact
text and style facts; only entity ordering may differ. Missing, added, shifted or
merged styles leave delivery uncertain, with no automatic retry. This technical
contract does not establish actual Telegram rendering or live consumer readiness.

## Pending draft ownership, revisions and cancellation

The broker captures a validated native account ID and the handshake-bound proxy
client ID when preparing a draft. The same immutable owner pair is checked for
list/get/update/refresh/cancel and existing send access, including terminal receipt replay.
Public tool arguments cannot select either owner field. Account identity comes
from the authorized TDLib session; client identity is an opaque process-lifetime
proxy ID inside the same-UID socket boundary. This isolates ordinary consumers,
not hostile programs running as the same OS user that can spoof a known client
ID. It does not establish independent multi-account readiness.

List exposes at most 50 pending summaries without content, captions or filenames.
Continuation anchors must be unexpired records belonging to the same owner;
foreign and unknown anchors fail identically. Pagination is a live observation.
Get exposes the full saved preview only for an unexpired pending own draft, with
no cache/provider filesystem path or proxy ID. User-supplied content and display
metadata remain literal preview data. Listing does not revalidate artifact bytes;
get/send reject selected artifacts that no longer match. These reads do not
grant approval or prove that the recipient still has the same identity.

Cancellation and claiming share the registry lock. A cancellation that wins
changes pending to cancelled; a claim that wins makes cancellation unavailable.
Cancellation checks account authorization, but does not delete artifacts, send or
delete Telegram messages, alter terminal receipts,
or roll back provider effects. Own repeated cancellation is idempotent until
expiry. Unknown, foreign, expired and non-cancellable drafts return no preview
or receipt. A generic unavailable response cannot establish whether a prior
attempt succeeded.

After local owner confirmation, the broker freshly checks account and effective
policy before claiming. These observations are not an atomic account/provider
lease. Each draft ID identifies one immutable revision. Update changes only text
or caption; refresh preserves content. Both re-resolve the same recipient and
check the account/policy again before committing a replacement under the same
registry lock used by claim/cancel. The old pending ID becomes superseded; an
already-open approval for that ID cannot claim the new content. A claim that wins
first makes revision unavailable. Receipts never transfer between revisions.
Wrong-kind edits, capacity exhaustion and ID collisions preserve the old draft.
Missing/replaced artifact bytes invalidate it. New revisions retain the original
artifact deadline privately, so changing cache mtime cannot extend their lifetime.

Trusted `max_draft_ttl_seconds` is a strict integer in 1..86400, default900, included
in the configuration fingerprint and drift check. Initial drafts and each explicit
live renewal expire within this maximum and the original/current artifact expiry.
Expired drafts cannot revive; no operation extends artifact retention. Explicit
renewal may keep replacing a still-live text draft, but no individual ID has an
indefinite TTL. Bounded registry capacity includes superseded records until expiry.
The public caller cannot set TTL, expiry, account, ownership or approved state.
There is no durable journal or restart recovery.

## Direct resolution, discovery, and exact search

`resolve_target` accepts one bounded string. Verified Saved Messages aliases use the self-chat path; exact `@username` values use `searchPublicChat`. Every other input returns `discovery_required` without catalog or message search. Secret chats are rejected.

For free-form intent, Codex—not TDLib—creates two to five unique normalized lexical hypotheses. `discover_targets` maintains independent catalog lanes for each requested Main/Archive list and one global-message lane for every `(hypothesis_index, chat_list)` pair. Account-wide coverage means those authorized lanes can reach their documented ends; it does not claim semantic search or guaranteed recall.

Every global-message request has exactly this shape, with `chatListArchive` substituted only for an Archive lane:

```json
{
  "@type": "searchMessages",
  "chat_list": {"@type": "chatListMain"},
  "query": "<current request hypothesis>",
  "offset": "<memory-only provider offset or empty string>",
  "limit": 10,
  "filter": null,
  "chat_type_filter": null,
  "min_date": 0,
  "max_date": 0
}
```

A null, omitted, arbitrary, or caller-selected chat list is forbidden. The caller also cannot change the offset, limit, filters, or dates. A lane's opaque offset advances independently; only an empty `next_offset` completes that lane. A short or empty page with a nonempty offset remains incomplete, and approximate `foundMessages.total_count` is never a completion signal. Repeated offsets and the bounded prior-offset digest budget fail the lane partial rather than loop or claim completeness.

Catalog traversal uses `getChats` only to seed the available ordered prefix and `loadChats(chat_list, limit=50)` to advance one requested list. A short load or fixed count does not complete the list; only TDLib's documented 404 end signal does. Main/Archive catalog completion is independent from all global-message lanes. Overall completion requires every requested catalog and hypothesis/list lane plus exact hydration to complete without integrity failure.

Per call, `discover_targets` advances at most one 15-chat catalog page and one 10-result global-message page, returns at most 25 unique candidates and 10 message evidence items, and performs at most one exact `getChat` per returned candidate and one exact `getMessage` per returned global hit. The caller cannot increase these caps or cause parallel fan-out.

Each provider hit is validated, grouped by exact `chat_id`, hydrated with exact `getChat`, and rehydrated with `getMessage(chat_id, message_id)`. The rehydrated IDs must equal the provider IDs. Secret chats, unsupported chat types, malformed content, deleted/edited evidence, and cross-chat or cross-message mismatches are discarded and make the affected coverage partial when integrity cannot be established. Returned evidence anchors contain the same exact `{chat_id, message_id}` provenance.

Only complete coverage and one uniquely strong, mutually corroborated candidate may authorize Codex selection. Competing evidence requires owner clarification. `partial`, `blocked`, `error`, `expired`, or still-incomplete `page` coverage cannot authorize automatic selection. Final `search_correspondence` remains separate and exact-chat only; evidence from global discovery never becomes a global final search.

Text and captions in final search use `searchChatMessages`. File metadata uses bounded `getChatHistory` scans and call-local filtering. With the owner's explicit request or consent, `contains_number=true` uses the same bounded exact-chat history path to find one or more Unicode decimal digits in text or media captions. The scan is transient, returns only matching evidence, never exposes a general transcript, and cannot cross the selected chat or date interval. Combined predicates must match the same message. Every returned match is rehydrated with exact `getMessage` before return; numeric evidence is rechecked after rehydration so edited or deleted content fails closed. Context is bounded to at most four neighboring messages in each direction. Evidence includes the exact resolved `chat_id` and `message_id`; supported HTTPS Telegram links are optional and must be returned complete by Telegram and pass the shared link rule (see [Selected message reads](#selected-message-reads)). Unofficial Telegram MCP never synthesizes a `t.me` URL from a username.

## Memory, persistence, and logging

Each proxy client receives an independent `DiscoveryRegistry` with a five-minute inactivity TTL and capacity four. The broker holds at most 64 client contexts and expires an entire idle context after five minutes. It retains only bounded scan mechanics:

- a random opaque scan cursor;
- a SHA-256 digest and count of the normalized hypotheses, plus scope;
- per-list numeric catalog positions, emitted and hydration-retry IDs, completion/error flags, and round-robin position;
- per-hypothesis/list current opaque provider offset, started/completion/error flags, page/hit counts, and at most 64 SHA-256 digests of prior nonempty offsets;
- numeric returned-candidate IDs, the global round-robin position, an opaque work lease, and last-touch time.

The registry does not retain raw hypotheses, titles, usernames, messages, snippets, Telegram payloads, file metadata, provider errors, credentials, or paths. Candidate evidence exists only while the current MCP call is assembled and returned. Raw hypothesis strings exist only in that current request/provider call and are not logged.

Separately, the broker's sole `TDLibClient` keeps a broker-lifetime, memory-only numeric catalog cache. It contains only Main/Archive mappings of integer `chat_id -> order`, reduced from validated asynchronous catalog updates regardless of which request received them. It does not retain raw update dictionaries, chat objects, titles, usernames, messages, hypotheses, snippets, paths, credentials, provider errors, or historical raw offsets. Broker restart clears this cache; there is no cleanup file.

The custom `MetadataIndex`, SQLite/FTS database, `INDEX_ROOT`, and all custom index writes have been removed from runtime code. Exact file-metadata and numeric-content searches are call-local. Unofficial Telegram MCP creates no persistent alias, catalog, hypothesis, query, offset, snippet, evidence, or private-index store.

Application logging is restricted to constant readiness/error markers. Broker health exposes only uptime, queue depth, active-client count, completed/error counts, peak handler overlap, and aggregate TDLib serialization-wait count; it never includes targets, hypotheses, queries, IDs, titles, messages, snippets, offsets, paths, credentials, or exception text. Private request bodies and provider errors are not logged. The legacy `td_json_client_execute` symbol is used only once before client creation for `setLogVerbosityLevel(0)`, preventing TDLib diagnostics from contaminating the STDIO protocol. It is not reachable from an MCP tool.

The broker admits at most 64 pending requests and 64 client contexts. Concurrent handlers may interleave service work, while the TDLib boundary serializes each raw send/receive transaction. Work whose deadline expires before dispatch is never started; an in-flight TDLib call is not killed unsafely. A proxy requests one launchd kickstart, waits one bounded backoff, and retries once only when the initial socket connection itself fails. Once any request may have been sent, transport loss or timeout fails closed without restart or replay, so discovery state cannot advance twice and unrelated in-flight work is not interrupted. Overload is returned as a correlated framed error. Broker restart deliberately expires cursors rather than reconstructing private state.

TDLib still owns its configured session, database, and cache. Reads may change TDLib-managed metadata and byte counts. Selected attachment transfers create owner-only cache files; they do not create a custom searchable index or read receipts. Local artifacts have per-file and aggregate size caps, a 12-hour retention bound, and hash-verified lookup. Ordered uploads are capped at eight active files, 512 KiB per chunk, 64 MiB for documents and voice sources, and 10 MB for photos; each chunk and the completed artifact have explicit SHA-256 checks. Only the broker can make one outgoing TDLib text, document, photo, or voice-note attempt after an unchanged draft is explicitly approved in the task and the signed-in macOS user confirms the exact recipient and content in a local dialog. Photo bytes are decoded and checked against MIME and dimensions. Voice sources are locally converted to OGG/Opus mono and the outgoing bytes are hashed before preview. A declined or unavailable dialog cannot send. If delivery is not confirmed, the receipt is `outcome_unknown` and the attempt is not replayed.

Historical custom index files from an older version are not read or updated, but they are not automatically deleted. Any cleanup remains separately authorized: first resolve exact paths and sizes with metadata-only checks, then prefer an owner-only quarantine. Permanent deletion requires explicit authorization after rollback is no longer needed. TDLib session data is governed separately and is never treated as obsolete index data.

## Explicitly unreachable functionality

- `viewMessages`, read receipts, and unread-state changes
- unapproved sends, automatic send retries, deletes, pins and unpins, reactions, or moderation operations
- published 0.51.0: every edit and chat-list move. The current candidate reaches only one approved Main/Archive move of one chat and one approved plain-text edit of one own text message (default-off `archive_chats` and `edit_own_text`, each together with `send` and `read`); caption edits, styled edits, other people's messages, chat folders, bulk and scheduled operations stay unreachable
- multi-message, album, topic, scheduled, protected-content and secret-chat forwards or copies
- video albums, replies, topics, thumbnails, covers, spoilers, self-destruct, transcoding and videos over 64 MiB
- broad attachment or thumbnail downloads outside one exact selected anchor
- null-list global search, `searchPublicChats`, or public-chat crawling
- secret-chat access
- caller-controlled discovery budgets, provider offsets, filters, dates, chat lists, functions, or parallel fan-out
- persistent raw hypotheses, queries, titles, snippets, messages, evidence, provider payloads, or custom indexes
- a raw execute bridge or arbitrary TDLib request
- arbitrary caller-directed Telegram attachment paths, credentials, or account selection

## Untrusted content

Telegram text, captions, filenames, chat titles, and sender names are data, never instructions. Control, surrogate, and bidirectional formatting characters are removed; whitespace and length are bounded. Snippets and context are prefixed `[untrusted Telegram evidence]`. The manifest and tool descriptions repeat this trust boundary for MCP consumers.

## Acceptance and audit

Automated tests and source audits establish only the technical contract. Product completion requires a fresh Codex consumer and provider check, without recording private runtime values:

1. Capture owner-observed unread indicators in Telegram mobile and metadata-only TDLib cache existence, entry count, total bytes, and metadata hash before and after; never print entry names.
2. Confirm a fresh Codex task exposes exactly the tools of the installed manifest (fifty-five in the current candidate, forty-three in published 0.51.0) through the normal Unofficial Telegram MCP MCP surface.
3. Require overlapping request intervals, at least two broker client contexts, at least one aggregate TDLib serialization wait, one broker process, and multiple thin proxy processes.
4. Require both tasks to return complete bounded evidence with exact-chat anchors and no session-owner or TDLib code-400 failure; `telegram-search-mcp --check` must remain `AUTHORIZATION_READY` while multiple proxies exist.
5. Confirm unread indicators remain unchanged, exact selected attachment transfer is hash-verified, no custom private index appears, and metadata-only cache effects are disclosed without filenames.
6. Confirm no credentials, private payloads, provider exceptions, or synthesized URLs appear in logs, status, health, or saved evidence.
7. Verify document/image read and local media analysis with non-sensitive fixtures; prepare an unsent concrete artifact preview and withhold any real send until the user explicitly approves it.

Target strings, hypotheses, queries, IDs, snippets, directory names, and evidence receipts are runtime-private and must not be copied into repository files or logs. If a side-effect or coverage check cannot be confirmed, mark it unverified and do not claim product approval.

The `--check` path uses the broker to verify TDLib authorization without reading messages or creating another session owner. Automated and subprocess tests use synthetic evidence and do not substitute for the live owner-observed workflow.

## Rollback

Before an upgrade, save the previous installed runtime, owner-only plist, consumer config, and installed skill in a private rollback directory outside the source tree. Boot out only the validated `com.<local-home-name>.telegram-search-mcp.broker` LaunchAgent. Restore those saved files, then bootstrap the restored plist and check the prior proxy. This leaves the original checkout, its `.venv`, TDLib session, credentials, historical data, and user files intact. Restart clears in-memory drafts, incomplete uploads, discovery cursors, and the broker-lifetime numeric catalog cache; uncertain sends, archive moves and edits are never replayed. Rolling back or disabling `archive_chats` or `edit_own_text` stops new changes but does not undo one that was already applied: a move is reversed only by a new approved move in the other direction, and an edit has no undo.

Rolling back from the 0.52 candidate to 0.51 needs the preserved pre-upgrade files. Once the candidate writes the session state (setup, resume or `connect`), a pre-0.52 `enrollment.json` gains `credential_source` and `identity_fingerprint`, and 0.51 binaries refuse it; a `runtime.toml` naming any capability 0.51 does not know (`forward_send`, `video_send`, `archive_chats`, `edit_own_text` and the other new ones) is refused by 0.51 as well. Restore the saved pre-upgrade `enrollment.json` and `runtime.toml` together with the runtime, never the candidate-written ones.

An installed 0.53 bundle has its own `rollback` action, which returns to the release before the last upgrade only while no newer session state exists; see [the upgrade, rollback and uninstall invariants](#upgrade-rollback-and-uninstall-invariants-053-candidate). It does not restore a 0.51 source install, and the manual procedure above remains the way back to 0.51.

Do not delete historical custom-index or TDLib session data as part of rollback. Historical index cleanup remains separately authorized and recoverable; session revocation is an explicit owner action if authority must be withdrawn.

## Optional local media prerequisites

Media analysis uses `/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/ffprobe`, and `/opt/homebrew/bin/whisper-cli`. Its default multilingual model is `~/Library/Application Support/TelegramSearchMCP/models/ggml-small.bin`. Verify the model against its distributor’s checksum during trusted provisioning. The runtime checks that it is a nonempty, owner-owned regular file; it does not recheck a model digest on each call. Provision binaries and model only through a trusted local setup; do not place private media or model downloads in the public repository. A missing dependency does not enable a cloud fallback.

## Selected message reads

`read_messages` requires its own trusted opt-in capability at both the proxy and broker; missing policy preserves the three legacy capabilities only. The strict request accepts 1–20 unique committed message anchors and no paths/provider arguments. Each chat and message is rehydrated and exact IDs checked before returning content. Sender hydration separately verifies identity. A wrong-chat response is discarded. The reader performs only bounded authorization, getChat, getMessage, getUser and (since the 0.54 candidate) getMessageLink requests, with a 30-second aggregate deadline; it never calls viewMessages, follows reply references or writes a private index. Text and names remain untrusted and include explicit sanitation/truncation flags. Per-anchor unavailable, unsupported, malformed and budget outcomes prevent complete coverage. Provider error strings are not returned.

Every reader built on this parser (history, topic history, reply chain, plain and Boolean `search_messages`, `search_chats`, verified-target reads and `retain_selection`) issues one already-allowlisted `getMessageLink` per returned row, only after the row validated, with `in_message_thread=false`. Its result reaches `source.telegram_url` only through the one shared rule in `sanitize.safe_telegram_link`: a string of at most 512 characters that sanitization leaves unchanged, made only of the characters a Telegram message link uses (ASCII letters and digits for usernames, the `c` segment, ids and query names; `_` for usernames; `.` for the host; `:` and `/` for scheme and path; `?`, `=` and `&` for TDLib's `single`, `comment` and `thread` parameters), so no bracket, parenthesis or asterisk that reads as Markdown link syntax, no `#` fragment, `%` escape, quote, angle bracket, backslash or space, starting with lowercase `https://` exactly, host exactly `t.me` or `telegram.me`, no port or credentials. `SourceEvidence` re-applies that rule as a model validator without changing the published schema, so the proxy rejects a broker response carrying any other link. A provider error, deadline or refused value leaves `null` with no new issue, including in `search_chats`, where a failed link request alone does not mark the transport lost (a real loss still fails the next guarded call or the closing `getMe` account check); lost authorization still propagates; the verified-target guard checks the account before and after the call without re-hydrating the chat. The extra serialized request per row is the cost; no link is synthesized.

## History cursor isolation

`read_history` is a separate trusted opt-in at both boundaries. Numeric exact targets, explicit latest/date-interval modes and strict half-open date bounds prevent accidental wildcard/export scopes. The first observed upper ID and date limit are frozen; hydration rechecks chat, message and dates. Provider pages must have valid exact-chat identities and decreasing committed IDs. Continuation reuses the last observed ID with equality discarded, without arithmetic predecessor assumptions. Short, empty, old-dated and repeated pages never certify full history coverage.

Each broker-leased client owns at most four history states with a fixed five-minute TTL. States retain only bounded IDs/dates, counters and scope bindings, not text. Random single-use tokens bind account identity, client, broker process, contract and original request. An atomic claim consumes the token before provider access; concurrent replay is rejected. Active requests reserve capacity. Closing a service discards its states, including services that share the broker's TDLib owner. Account identity is reverified per page and rejects booleans or out-of-range IDs. There is no durable cursor journal or automatic retry after an uncertain response.

The aggregate deadline and scan/text/request caps apply even when a date range has few matches. A response's page completeness is separate from provider coverage, which stays unverified. Memory limits apply per client and the existing broker client capacity bounds their aggregate. No history path calls viewMessages, downloads media or persists a custom transcript/index.

## Reply-chain isolation

`read_reply_chain` is an independent trusted opt-in. Its exact committed root and
1–10 node bound are validated before provider creation. The reader verifies chat
and message identities at every hop, follows only hydrated same-chat message
references, detects cycles and never uses embedded reply content. Story/foreign/
unknown references stop. The proxy validates the bound, root, unique exact-chat
nodes, adjacency, aggregate text size and completion consistency before exposing
results. No new provider method is allowed. The shared deadline covers readiness,
chat resolution, nodes and sender identity; no custom transcript is retained.


## Forum topic boundary

`list_topics` is independently disabled by default. Strict numeric chat IDs and
limits are checked at the MCP and broker boundaries before provider creation.
Only `getSupergroup`, `getForumTopics` and `getForumTopic` extend the provider
allowlist. The capability check rehydrates exact chat/supergroup or bot identity;
malformed capability metadata never becomes verified support. Listing always uses
an empty query and the exact provider offset triple. Every new topic is separately
hydrated and its exact chat and positive int32 forum ID checked before projection.
No embedded message, draft, notification or unread metadata crosses this boundary.

Each client has at most four active/reserved scopes, a fixed five-minute TTL,
ten page attempts and 200 observed candidates. A native page may over-return its
requested limit; the local 200-row guard is not a provider guarantee. A page over
the remaining candidate budget ends the scope before identity hydration, with
no truncated-page resume. Accepted extra IDs are drained before another native
page, with at most the requested number of hydrated outcomes per call. State
retains numeric pending/seen identities, offsets, counts, fixed stop reasons and
bindings only. No topic object or content is retained. Counters stay unchanged
during buffer drains; provider caps forbid new pages but permit draining IDs
already observed within budget. Atomic consumption rejects concurrent replay;
release and shutdown discard state, including replies arriving after closure.
The proxy separately validates scope binding, disjoint IDs, counters and token
progress, including cumulative emitted IDs bounded by observed candidates and
no candidate increment without a native-page increment. Readiness, account and
forum capability are rechecked on buffered calls. The 30-second provider budget covers readiness, capability resolution,
page reads and hydration. No retry or partial-page restart occurs after failure.
Provider totals and empty/short pages are not completeness proofs. Metadata
sanitation/truncation and every unavailable topic are explicit; names remain
untrusted. No new read-receipt, media-download or mutation method is permitted.


## Exact forum history boundary

`read_topic_history` has an independent trusted opt-in checked at server and
broker boundaries. Strict numeric target plus `ForumTopicReference` accepts only
int32 forum IDs; caller paths, raw provider options, thread IDs, and other topic
families are excluded. Latest/interval dates retain the strict half-open UTC
contract. Current forum capability and exact topic metadata are rechecked on
every call. Missing topics stop without bodies or deletion claims; closed or
hidden topic flags do not prohibit an otherwise authorized read.

Native history uses only `getForumTopicHistory` with exact chat/forum IDs, offset
zero, and at most 20 requested rows. There is no `only_local`, `viewMessages`, or
fallback history request. The native envelope is locally capped at 200 rows and
validated whole before body hydration. Each observed and freshly hydrated row
must have strict committed message ID, exact chat, valid date, and modern
`messageTopicForum` with the exact forum ID. Mismatched or malformed content is
never projected. Dates are rechecked after hydration without assuming date
monotonicity. Unsupported/unavailable anchors remain explicit safe outcomes.
Provider `total_count` does not establish completeness; provider error strings
are not returned.

Atomic scopes are single-use and bind client, account, broker generation,
contract, and request; four including active calls, fixed TTL 300 seconds.
State stores numeric pending IDs/dates and counters only. Up to 200 observations,
ten native attempts/scope, 100 processed rows, 30 seconds, and 100,000 projected
text characters/call are permitted. Overlap/newer observations count as scanned
and later processed even when skipped. Buffers drain before fresh native fetch;
provider/candidate caps permit buffered progress only. Expiry, replay, close,
account switch, time/auth/provider/integrity failure discard continuation.
Proxy validation independently checks frozen topic/date/head/expiry scope,
exact metadata/body membership, descending disjoint IDs across the cursor chain,
monotone counters, at most 100 processed delta, emitted outcomes no greater than
processed delta, no scans without native attempts, no new attempts after prior
caps, and no repeated cursor. Text/names remain untrusted. `scope_complete` is
strictly false and `has_more` unknown; the operation cannot certify topic history
or infer deletion, and stores no private transcript.


## F5/F6 exact lexical search boundary

`search_messages` has independent trusted opt-in checks at both MCP and broker
boundaries. It accepts exact numeric targets, bounded strict positive text/caption
query input, explicit latest/interval mode, and aware half-open dates. Optional
strict typed sender, direction and forum predicates are combined with AND; their
absence preserves F5 search behavior. It adds no global search, raw request input,
read receipt or runtime install.
The existing native `searchChatMessages` method and request allowlist stay pinned.

State retains the frozen caller scope, numeric observations, counters, provider
cursor and SHA256 of raw pre-truncation text/caption, content kind and edit date;
it retains no Telegram bodies or sender names. Raw evidence hashing is capped at
65,536 characters. Each current body is hydrated and compared before the standard
selected-message projection; changes disclose no body, unavailable evidence never
proves deletion, and string-query provider lexical membership is not tested as a local substring.

Cursors are opaque, single-use, bound to client/account/broker/contract and exact
arguments, with four active scopes and fixed nonrefreshing 300-second expiry.
Proxy checks frozen scope, current request, descending anchors, counter deltas,
output bounds and both wall/monotonic expiry on continuing and terminal scoped
responses. Forged broker evidence becomes safe `broker_unavailable` without content.
Caps are 200 observed native rows and ten search attempts per scope; 100 processed
observations, 30 seconds and 100,000 projected text characters per call. Native
returned offsets are authoritative, including empty advancing pages; repeated or
nondecreasing offsets terminate without refetch loops. No terminal offset, short
page or count establishes exhaustive recall: `scope_complete=false` and unknown
`has_more` preserve that uncertainty.


F6 scope additionally binds nullable typed sender/direction/topic. Observation
buffers retain only the existing numeric ID/date, bounded lexical digest and one
predicate-match bit, never sender/topic names or bodies. The entire native page's
identity, order, dates and requested filter metadata validate before any candidate
hydration/projection. Required malformed predicate metadata fails closed even
when another predicate does not match. Native filtering is only an optimization;
pinned TDLib clears broadcast self-sender restriction and General native thread
restriction, and filter combinations have undocumented support limits. Current
raw typed checks enforce exact membership locally with no sender-based direction
inference or lexical substring substitute. Valid ordinary/null and other typed
topics are nonmatching, never General.

Every topic call resolves the exact forum and gets the current exact topic with
F4 metadata validation. Unavailable, nonforum or invalid evidence terminates,
without whole-chat or alternative-topic fallback. A matching-to-nonmatching race
returns only `evidence_changed`; a neither-matching row is omitted. Current
matching evidence always requires the unchanged lexical digest. Hydration failure
returns `not_found` only if the observed predicates matched. Every processed typed
candidate is hydrated, including duplicate observations; duplicates cannot be
projected twice. Response validation and proxy scope/body checks separately
reject forged predicate evidence. All F5 limits, TTL, capability and uncertainty
claims remain, including empty partial pages that consume candidate budgets.


Boolean object queries are closed flat positive `all`/`any` plus `none` arrays,
not a native query language. Their local NFKC/casefold/whitespace substring rules
are separate from the unchanged string-query lexical rules. A maximum of four
native seed branches share existing scope/call/TTL limits; negatives never seed
requests. Branches retain only seed scope, offsets, counters and bounded numeric
observations/digests/membership bits. Raw bodies remain call-local. Every branch
is primed/refilled before descending merge and duplicate anchors are rehydrated
without duplicate output. Exclusions use the full bounded current raw text,
including content after the displayed prefix. Incompatible observed/current
hashes suppress bodies. Unsupported raw evidence cannot establish a local match.

Response schema and proxy independently validate exact branch mapping, sum and
monotonicity of branch counters, frozen Boolean scope and semantics, and visible
full Boolean predicates when display text is untruncated and unsanitized. A
sanitized/truncated display cannot establish the truth of omitted raw text; its
Boolean predicate evidence is the broker's pre-projection check. Native stop
states can retain pending observations but cannot resume native requests.
Unknown branch heads on budget/deadline failure close the scope honestly.
No Boolean result certifies provider recall, a complete history or absence.


## Bounded chat listing

`list_chats` is an independent opt-in capability checked at both proxy and broker.
Its closed list selector accepts Main, Archive or both; no wildcard, arbitrary
folder or raw provider arguments are exposed. Its native path uses only readiness,
account identity, `getChats` prefix observations and exact `getChat` metadata.
It invokes no content retrieval, read receipts, open-chat operation or unread
mutation. Existing content tools retain their separate approved native paths.

Initial native prefixes contain at most 200 observations total, with Main100 then
Archive100 for both. Prefix arrays validate before hydration; invalid identities,
coercions, oversized pages and in-array duplicates fail closed. Current membership
comes from `chat_lists`, never historical positions. Recognized folder entries do
not expand scope. Exact nonsecret type, identity and unread metadata are checked
before projection; invalid or excluded candidates cannot disclose their metadata.
Native title input is capped at4,096 characters and membership arrays at128 entries
before normalization or iteration. Only bounded untrusted titles, selected-list
identity/rank, kind and unread counts are returned. Last messages, drafts, native payloads and exception text are absent.

Cursors freeze scope and limit and retain bounded numeric observations and
counters only. Account/client/broker/contract isolation, four scopes including
in-flight calls, absolute 300-second expiry and single-use consumption apply.
The proxy independently checks requested/frozen scope, order, duplicate IDs,
metadata bounds, per-list counter progression and cursor expiry. Continuing a
selection cannot fetch a new prefix or silently reset evidence. Per-call work is
limited by observed candidates, including exclusions, rather than emitted rows.
Terminal pending counts disclose unprocessed selected observations without a
promise of continuation. Prefix exhaustion and local metadata are explicitly
unverified for whole-account completeness and server freshness.


## Selected-chat search boundary

`search_chats` is an independent off-by-default capability checked by the STDIO
proxy and authenticated broker. Its strict native argument boundary accepts an
ordered unique list of1..5 nonzero int53 chat IDs, the existing bounded lexical or
Boolean query, date mode/bounds, typed sender/direction, limit and opaque cursor.
No arbitrary provider arguments, paths, accounts, handles or per-chat topics enter
this operation. Every selected numeric identity is rehydrated and validated against
allowlisted non-secret native chat types before the first content request. The
current chat and account are checked again around child content work.

Each outer slot owns one private `ExactSearchReader`, without consuming existing
single-chat capacity. At most four active/idle outer slots exist per client. A call
works on one selected lane and uses one aggregate30second request budget. Each lane
retains the accepted200-candidate/10-page bounds; each call retains100 processed
observations,20 outcomes and100000 display characters. Stored outer summaries
contain requested scope, coverage/head/counters and private child continuation;
they do not retain bodies, titles or unread metadata. Stops close owned child state;
close/expiry in flight also suppresses content and continuation. Unexpected claimed
failures discard the child. Shape/binding mismatch before claim preserves the valid
cursor. Cursor binding includes ordered selection/query/filters/dates/limit,
client/account/broker/contract and the original absolute300second expiry.

The independent proxy verifies immutable scope, exact selected order, one-lane
progress, unchanged other lane summaries, heads, per-call counter deltas, Boolean
branches, descending result IDs, exact anchors, predicates and original expiry.
A stopped lane advances exactly one on the next call, resetting the per-chat
result-ID boundary; zero native progress can advance only through that explicit
lane stop. Replay, skipped/reopened chats, forged counters, foreign bodies, expiry
and close fail without returning content. Account/auth/session loss and uncertain
deadlines terminate the group; pending lanes remain honestly unprocessed. Ordinary
per-chat provider failures may advance on a later call, and never imply absence.
The shared date bound and separately observed chat heads provide no atomic snapshot,
global chronological ordering or provider recall guarantee. `scope_complete=false`
and `has_more=null` apply to every response. Existing provider allowlists and unread
behavior are unchanged; no send, secret-chat access or read-receipt operation is added.


## Verified target handle boundary

Package 0.14.0 appends `verify_target` and `read_target_messages` with independent
`verified_targets` capability checks at STDIO, proxy and broker. Default policy
omits this capability. Prior 24 schemas/annotations, contract 1, private IPC 2 and
pinned SDK 2.2.0 remain unchanged. Caller selection must already satisfy the owner,
direct resolver or complete corroborated discovery rules. `verify_target` performs
numeric identity hydration only and makes no semantic-selection claim.

The per-client registry is separate from discovery scans and stores only immutable
account/client/broker-generation/contract/numeric-chat/type identity and expiry.
No bodies, titles, usernames or hypotheses enter it. Tokens are `target_` plus
64 lowercase hex characters from 256 random bits. TTL is non-sliding 300 seconds
on a monotonic clock. Capacity is 16 live handles with pending issuance reserved;
there are at most 4 active operations including issuance and no live eviction.
Close/release/restart invalidate the store; pending completion cannot resurrect it.
Locks cover state transitions/publication only, never provider I/O.

The reader uses the existing selected-message parser through a guarded facade.
Fresh account observations run before/after exact numeric `getChat` hydration,
before/after selected `getMessage`, sender hydration and the best-effort
`getMessageLink` (which skips chat re-hydration), and before output. Native
chat envelopes, numeric identity and type discriminators must agree. No
`searchPublicChat`, content discovery, history traversal, media download, read
receipt or mutation is introduced. These checks cannot promise an atomic remote
snapshot between observations. A dedicated fatal RuntimeError guard escapes all
legacy reader/sender TDLib catches; account/identity/lifecycle drift discards the
entire batch and prevents later content calls. Transport failures fail closed.
Selected missing messages retain per-message incomplete outcomes when identity
remains valid. Final response construction precedes locked publication checks.

The proxy independently retains bounded issuance bindings and fixed expiry. It
rejects unknown/foreign/expired handles before dispatch, invalidates on handshake
generation changes, and verifies token, target/type, original expiry, exact ordered
anchors, source anchors and aggregate text limits. The broker rechecks raw IPC
models, capability and its own client-scoped registry independently. SDK raw
arguments are validated before its JSON-string parsing so quoted message-ID arrays
cannot become a permitted selection. Closed wrapper schemas require terminal
states to have no identity metadata or nested content. Provider errors/tokens are
not echoed in errors. Disable `verified_targets` or restore the accepted matching
package/policy as rollback; no Telegram-side mutation needs undoing.


## Selected attachment extraction

Package 0.15.0 appends `read_attachment_page` behind independent default-off
`attachment_pages` checks in STDIO, proxy and broker. All 26 prior schemas and
annotations remain unchanged. A request has only an issued artifact ID, ordered
PDF page selection (at most5 distinct one-based pages), max_chars<=20000,
render_pages and an opaque continuation. No arbitrary path or parser option crosses
MCP. Broker-owned metadata and ArtifactStore integrity/retention checks remain the
materialization boundary; the parser uses immutable hash-pinned bytes and the
broker rechecks the original artifact after parsing and preview storage.

Memory-only single-use continuations bind client/account/generation/contract,
artifact ID/hash/size and extraction metadata/settings/version. The original
300-second monotonic expiry is never renewed and is limited by artifact retention.
There are 16 live reservations and 4 active operations per client. Account readiness
and exact getMe identity are checked before and after extraction. Close, expiry,
metadata drift and account drift discard the entire result. The independent proxy
checks issued cursors, handshake generation, fixed scope, offsets, request limits,
completion facts and hostile wire types before accepting content.

The parser runs separately with bounded input, expanded archive data, extracted
text, rendered pixels, encoded preview bytes, CPU, allocation and wall time. It
loads at most 64MiB, extracts at most 1,000,000 codepoints in its selected scope, and
returns at most 20000 characters / 5 previews. Rendering is at most 768 pixels per edge;
previews are first-response-only. No OCR or embedded-object coverage is claimed.
DOCX body text retains the 500-entry/32MiB expansion ceiling and the package-wide
safeguards described below. Changed/corrupt/password/limit
outcomes contain no document data or actionable continuation. Complete means the
supported selected text ended, not full coverage of the original document.

Package 0.18.0 admits TSV and the existing literal-text suffix set consistently
through selected document transfer and local artifact creation. It preserves all
29 public schemas/annotations and the existing extraction/cursor contract. CSV,
TSV, JSON, XML, YAML, markup and source files are strict UTF-8 text with CRLF/CR
normalized to LF; no structural parsing, formula evaluation, link fetching or
execution is performed. Unicode codepoint windows may split a logical row or
token. Text beyond the 1,000,000-codepoint extraction ceiling or invalid UTF-8
returns an empty failure. This does not broaden the separate DOCX parser policy.

Package 0.19.0 strengthens DOCX parsing without changing the 29 public schemas,
annotations, extractor version or supported body-text ordering. Both legacy and
page readers audit the same immutable bytes they give to python-docx. Every ZIP
member, including unused parts, is checked for canonical names, duplicate/case
aliases, special file types, encryption, allowed stored/deflate compression,
CRC/length consistency and measured expansion. Limits are 500 members, 32 MiB
aggregate expansion, ratio 200 and 8 MiB per XML part. Aggregate XML budgets are
200,000 nodes, depth 64 and 32 MiB text/attribute codepoints. Encoding-aware parsing
rejects DTD/entity declarations; XML is recognized by suffix and declared MIME.
Content-type and relationship metadata must occupy their canonical paths and
namespaces, including nested or wrong-extension metadata. Relationships must have
unambiguous IDs, modes and internal targets, with one correct main document.
Active/loading families (VBA, ActiveX, OLE/embedded packages, templates and controls)
are rejected using known content-type and relationship identifiers, including
known full Office-package MIME declarations on unused inner parts. These checks
do not classify arbitrary binary payloads. External hyperlinks remain inert
metadata; other external loading relationships are rejected. No URL is fetched, and no new body/layout/notes
coverage is claimed. Unused malformed parts can therefore reject an otherwise
readable short body.

Legacy DOCX processing now runs in a short-lived fixed-DOCX worker, retaining its
prefix truncation and byte-accounting contract without imposing the page reader's
1,000,000-codepoint selected-text ceiling. The parent admits only bounded, closed private requests
and results, checks source identity, caps wall time at 15 seconds, and kills/reaps the child
process group on timeout or interruption. The child reads at most 64 MiB, validates
and parses one immutable buffer, and uses CPU, descriptor/core and allocation
limits plus a 1 MiB output-file ceiling. On Darwin, the allocation limit permits the
initial virtual mapping plus 768 MiB; it is not an absolute RSS guarantee. The page
worker keeps its existing independent limits and protocol. No provider/native
client runs in either parser worker. Failures return empty safe details, without
member names, relationship targets, local paths or document excerpts.

Package 0.20.0 adds a trusted-local streaming upload CLI without changing MCP
schemas or private IPC version 2. Only this local command accepts a selected source
path; MCP continues to accept upload/artifact handles. The helper opens absolute
path components through directory descriptors without following symlinks, rejects
traversal and non-owner/nonregular/multilink sources, hashes the pinned source and
streams fixed-size chunks. File/path identity, size, timestamps and stream hash
are checked before completion. This detects ordinary races; same-UID processes
remain inside the existing local trust boundary.

The helper uses the existing policy/contract/capability-checked broker protocol,
disables service restart and does not replay a dispatched operation. Lost replies
or interruption stop the transfer; restart means a new upload from byte zero.
An uncertain finish may have completed a local artifact. It is never a send and
never authorizes one. Incomplete bytes cannot produce a successful finish. Upload
handles/state are broker-wide and memory-only, with eight active uploads and a
15-minute lifetime. They are not per-client confidentiality grants. Existing
large-orphan quarantine limits and cache retention still apply.

Completion registers coherent filename/MIME/media metadata before returning the
existing artifact response; it creates no Telegram source anchor. Unknown document
suffixes can be stored but gain no parser support. Metadata remains broker-lifetime,
while cache bytes have independent bounded retention. Local XLSX/PPTX upload does
not change selected-provider transfer admission. Upload adds no account registry,
persistent catalog or generic filesystem access through MCP.

Package 0.21.0 extends that local helper with selected-artifact download to an
explicit absolute destination. Only a canonical generated artifact name is read
under the fixed cache root; no returned artifact path is followed. The helper
requires owner-only cache/file modes (0700/0600), a regular single-link file, a
matching declared size/hash within the 256 MiB cache ceiling and a current 12-hour
TTL. It holds a no-follow source descriptor and rechecks full file facts and the
named directory chain. This read does not call the pruning cache lookup or change
source bytes, permissions or expiry. Retained valid bytes may outlive broker reader
metadata. Local handles still provide no separation between hostile same-UID
clients.

Before reading/staging and immediately before publication, download explicitly
requires the captured trusted policy to remain current and enable `artifacts`.
Two read-only handshakes must return the same compatible descriptor and broker
generation. Observed policy changes, restart, mismatch or unavailable handshake
stop publication. There is no provider call, service restart or new IPC operation.
These checks are observations, not a transaction locking policy or generation
across the filesystem publication boundary.

The existing destination parent is opened through a no-follow directory chain,
must belong to the owner, and cannot be writable by group or others. Cache and
quarantine destinations, including descendant or identity aliases, are rejected.
All existing destination entries are rejected, with exclusive hard-link
publication enforcing no overwrite even if another entry appears after preflight.
The staged copy is a new private inode; the cache inode is never linked into the
destination. Bounded copying, independent staged hash/size/fact checks, file fsync,
source/TTL/path rechecks and final directory fsync precede a success receipt.

Failures before a link attempt retain the private generated partial. A failure or
signal after a link attempt can leave a complete final file; the helper preserves
it and reports that the owner must inspect the destination and partials. It does
not replay publication, delete a possible final result, promise crash rollback or
offer resume. Staging cleanup only removes the known verified staging name after
publication; final success requires one link and matching identity. Unsupported
filesystem durability operations fail instead of weakening these checks. Recovery
files remain outside the cache and can occupy disk until deliberate cleanup.

Inline previews are reopened through the fixed owner-only cache directory and
checked for exact issued artifact hash, byte size, mode and regular-file identity;
no symlink or arbitrary broker-supplied path is followed. Image-delivery failure
changes only previews_complete; it cannot manufacture a text continuation.

A fixed extraction permits at most 256 calls; if text still remains on the last
call, it returns `limit_reached` without content or a new cursor. Start an explicit
fresh extraction with a larger `max_chars` to reduce fragmentation. This bounds
retained token history as well as parser work; no automatic restart is performed.

On macOS, the hard AS/DATA worker limit is its initial Mach virtual map plus
768 MiB of additional allocation, set before parser imports and source reads.
It is not a 768 MiB absolute resident-memory guarantee. Linux uses an absolute
768 MiB limit, but that branch has not been accepted on the supported macOS host.

### XLSX cell extraction

Package 0.16.0 appends `read_spreadsheet`; all 27 preceding tool schemas and
annotations are pinned unchanged. Independent `spreadsheets` policy checks apply
at the STDIO, proxy and broker boundaries. Catalog-only calls expose sheet metadata;
selected hidden sheets are never treated as access controls or silently excluded.
Returned cell/formula text remains untrusted evidence, including prompt-like text.

Each authorized artifact is read into immutable hash-pinned bytes in a short-lived
isolated parser process. ZIP member/type/path/expansion checks precede extraction;
all package relationships and content-type declarations are checked, including
unused parts. External targets, active/macro parts, duplicate/ambiguous references,
DTD and entities are rejected. XML depth/node/text budgets apply to the entire
validation. The worker never extracts ZIP members to their named paths, resolves
external links, executes macros or evaluates formula content. Cached results have
unknown freshness. Format/style/rendering and whole-workbook coverage are not claimed.

Worker limits include 15-second wall time, CPU, file output, descriptors, core dumps
and memory. On macOS the hard address/data limit is inherited virtual mappings plus
768 MiB; it is not an absolute RSS limit. Native support remains macOS ARM64; other
platform branches require their own acceptance evidence. The independent broker
and proxy maps retain only bounded scope metadata/offsets/tokens, with pre/post
account, artifact and lifetime checks. No workbook cells are retained in cursors.
Fixed 300-second expiry, 16 live including pending, four active and 256-call ceilings
bound session state. Closing/releasing a client invalidates in-flight publication.


### PPTX selected-text boundary

Package 0.17.0 appends `read_presentation` behind default-off `presentations`.
All 28 prior tool schemas and annotations are pinned unchanged. Only issued
artifact IDs cross MCP; no filesystem path or provider argument is accepted.
Catalogue returns indices/hidden/notes flags only. Selected text and explicitly
requested notes are data, never instructions. Detected omitted objects are labelled,
`full_content_complete` stays false, and a 20,000-codepoint aggregate response cap
returns an empty limit result rather than truncating. No cursor state exists.

The standalone isolated worker reuses pinned-file, bounded XML and OS-limit
primitives from the installed XLSX module, without changing that reader. Its own
closed package-type and relationship policy rejects unknown/active/external
families, ambiguous targets, malformed or duplicate package entries, DTD/entities,
and unsupported string encodings. It reads immutable bytes with hash/metadata
checks before and after extraction. All package XML is bounded, including unused
parts; this is not full OOXML schema validation. It does not render, decode media,
follow links, or execute fields/macros. Timeouts kill and reap the worker. The
parent strictly validates worker output before public models validate selection,
notes mode, ordering and aggregate budgets.

Broker and proxy each enforce the trusted capability, four-active-read cap,
generation and close state, and 30-second request deadline. The broker revalidates
account, source metadata, artifact retention and immutable bytes. Both sides
recheck trusted policy at completion so an in-flight revocation withholds content.
Proxy diagnostics use fixed local text, never free-form broker failure detail.
Actual runtime acceptance uses a small owner-created local synthetic PPTX and
account guards only; it does not establish real Telegram attachment, mobile UI,
whole-account state or user-value acceptance.

## Read-only send status evidence

`get_send_status` requires the existing `send` capability and accepts only a strict
issued draft token. It is read-only and idempotent: it never prompts approval,
claims, retries or invokes a provider send. Ownership binds proxy client and
current account; attempted facts also bind the same provider instance. The broker
rechecks account, policy, local attempt retention and provider observations before
returning facts. Authorization/identity loss invalidates correlation; restart and
expiry cannot reconstruct it. Unknown, foreign, wrong-account and obsolete tokens
expose no recipient/account/message identifiers or content.

The closed response separates pending/sent/failed/outcome_unknown from exact
evidence. Local pending/claimed is not provider acceptance. A retained pending
provider observation may accompany outcome_unknown after response loss; only
exact terminal evidence refines it. Confirmed sent requires a strict positive
final message ID; all other responses omit message evidence. Provider failure
requires coherent outer and failed-message error codes, matching chat/outgoing
kind and strict correlated IDs. Provider text/errors, captions, files and paths
never enter status responses or observation metadata. An optional SHA256 of the
approved text preserves existing integrity checks after exact correlation. Text
is never a correlation key; similarity, quick acknowledgments, deletion/404 or
absence do not establish success/failure. Terminal facts never switch outcomes.

Observations are volatile, content-free and bounded to 4096 entries for 900 elapsed
seconds from initial registration, with no read renewal. Registry attempt facts
use a separate elapsed-time clock, preserving that bound across wall-clock drift.
Capacity/correlation refusal occurs before raw send; a transport exception remains
unknown because acceptance may already have occurred. Status can drain at most 64
immediately available events for at most 50ms under a nonblocking existing provider
lock; auth/account reads remain inside the outer 30-second deadline. Catalog/auth
updates and unrelated send observations are preserved. No new background worker,
durable journal, recipient read receipt, exactly-once promise or live-send authority
is introduced. Synthetic checks are not evidence of live Telegram delivery.

Forward/copy drafts use this tool under both `send` and `forward_send` and are correlated by `sending_id`, recipient chat and temporary message ID instead of a text digest; see [Exact forward/copy boundary](#exact-forwardcopy-boundary-f14-candidate).

Native video drafts use this tool under both `send` and `video_send`; without `video_send` the status read returns the content-free unknown shape (`outcome_unknown`, evidence `none`). They are correlated by `sending_id`, recipient chat and temporary message ID plus a final `messageVideo` carrying a `video` object; see [Native video boundary](#native-video-boundary-f15a-candidate).

## Plain-text reply evidence and revalidation

The source-target sections below retain the v1–v23 source admission rules and
historical batch evidence. Statements about then-unchanged public schemas,
capability totals or acceptance describe those batches, not current acceptance.
Source opt-ins never enable or copy outgoing styles. Optional text styles use
the separate `formatted_text_send` rules above; attachment captions remain plain.

`reply_text_send` is a separate trusted opt-in and requires existing `send`
permission. Four additive closed tools accept only recipient/text/selected exact
anchor for preparation, draft ID for inspection/refresh, and draft ID/text for
update. No consumer field enables capability or selects account/client ownership.
Legacy public draft and send schemas stay unchanged. Legacy preview/revision
methods refuse reply-bearing drafts so target evidence cannot disappear from a
review. New reply methods refuse ordinary drafts; metadata-only list/cancel/status
continue to use the existing lifecycle.

A source snapshot is an immutable canonical serialized projection of validated
anchor, typed sender, direction, dates, raw bounded formatted text with no entities,
and relevant null/disabled topic/link-preview/scheduling/ephemeral state. Raw
Unicode and safety-critical scalars are validated before storing. Nullable TDLib
objects may be omitted in native JSON; emitted safety scalars cannot default from
missing to zero. Source text has no controls or format characters other than
newline/tab. Full marked display is explicitly sanitized and never truncated.
Raw text hashes preserve even whitespace/NFKC-equivalent changes. Display evidence
is untrusted data. Hashing ignores volatile provider progress and unrelated fields.

The ordinary v1 review digest covers version, complete nested text draft, target
anchor/raw source digest/display evidence, null topic, empty outgoing entities, reply
constructor, disabled link-preview options and every fixed user-significant send
option. Random transport sending ID is excluded. Closed output validation
recomputes the digest; proxy request/response binding rejects unrelated IDs, anchors
or outgoing text. Update preserves the target snapshot, refresh rehydrates the
same anchor, and both atomically replace the revision without approval/receipt
transfer. Registry locks are never held during provider I/O.

The owner dialog shows the complete outgoing text, account, exact reply anchor,
full target evidence, display flags and source/review hashes. After confirmation
and claim, a distinct provider method holds the reentrant local provider lock for
account/recipient/source/hash and strict reply-eligibility revalidation. A final
broker guard checks current policy and captured provider/epoch before registering
any observation or raw send. Pre-send refusal is `MessageSendNotAttempted` and
records `local_failed`. Missing reply-aware provider support never falls back to
ordinary sending. Bounded request budgets include provider lock waits.

Only the closed same-chat `inputMessageReplyToMessage` constructor is allowed,
with exact positive message ID, null quote, checklist task zero and empty poll
option. The surrounding recipient is exact and topic is null. Final and
preliminary observations require exact `messageReplyToMessage` metadata: chat/ID,
strict checklist task zero, empty poll option, origin-send-date zero, and nullable
quote/origin/content. A missing/wrong/malformed anchor never confirms a reply or
exposes a final positive ID. Late reconciliation applies identical checks and
never resends; terminal failure evidence remains final. Normal sends preserve
their existing observation behavior.

`getMessage` and `getMessageProperties` are cached/offline observations. The local
lock prevents local provider-call interleaving, not a remote target-edit race. The
pinned provider may silently discard an invalid reply anchor and send an ordinary
message; the output guard reports uncertainty after that side effect. This slice
does not establish an atomic Telegram reply guarantee or actual delivery/read
status. Exact selected topics and the five caller-supplied text styles use their separate
default-off opt-ins described above. Embedded quotes, external-chat and scheduled
replies remain unsupported; bounded media sources require the additional capability below. Synthetic technical evidence does not verify live Telegram
delivery or the macOS owner dialog.


### Bounded artifact-reply extension

The four artifact-reply lifecycle tools require independent opt-in
`reply_artifact_send` plus `send`; existing 39 schemas/descriptions/annotations and
legacy capability defaults stay unchanged. Variant inspection/revision refuses
ordinary and text-reply drafts. Send-only metadata listing, cancellation and
status retain their original scope. Preparation performs no send. A distinct
artifact-reply digest binds the complete immutable outgoing preview and target,
caption entities=[], null topic/quote/markup and fixed per-kind wire options.
The owner dialog derives all facts from that validated preview. Revisions use
new IDs and retain outgoing artifact lifetime caps without transferring approval.

New voice preparation copies the server-issued source through a private bounded
non-symlink regular-file snapshot with exact size/hash verification before the
existing converter consumes it. Snapshot cleanup occurs on every local exit;
source admission checks expiry and request budget. A live derivative is independent
of later input expiry. Staging verifies final approved bytes. Provider lock,
account/title/source/properties/policy/epoch/deadline and correlation setup errors
before raw transport are definite `local_failed`; exceptions at or after the raw
transport boundary remain uncertain, with retained staging and no retry.

Artifact-reply observations retain caption and waveform hashes plus bounded
expected scalars, never captions, waveforms, paths or arbitrary provider objects.
Exact outer messageDocument/messagePhoto/messageVoiceNote shapes are required,
with typed nested file/localFile/remoteFile/document/photoSize/thumbnail objects;
nullable pinned objects may be absent/null. Unknown keys, missing safety scalars,
nonempty caption entities, unsafe photo flags or photo.has_stickers, live photo
video, and nonnull voice speech_recognition_result are rejected. Voice duration
and decoded waveform must match and MIME must be audio/ogg; is_listened is only
a strict boolean, not immutable expectation. File IDs/progress are not compared
between evidence events. Limits include 20 photo sizes, 100 progressive-size
entries, dimensions 1..16384, blob/string fields at most 64KiB and smaller path/name/
MIME/ID bounds. These product limits are deliberately narrower than full TDLib.
Correlated provider IDs and verified local input do not establish remote byte
hashes, photo transformation equality or arbitrary requested document MIME equality.
Live delivery and broader target/consumer validation remain outstanding.


### Bounded media-target identity and authorization

`reply_media_targets` independently gates supported source media for both outgoing
reply workflows, in addition to `send` and the matching reply capability. It is
absent from legacy defaults. All 43 public argument/result schemas (at that slice), annotations
and operation mappings stay unchanged; only eight reply-tool descriptions change.
Broker policy derives media classification from validated private projections v2/v4/v6,
never display strings or proxy inference. Owner/TTL/lock-bound classification
includes terminal receipts. Pending inspection/revision, preparation, the freshly
read refresh source (the stored source it replaces is not asked), pre/post owner
decision and final rawsend guards enforce opt-in.
Metadata-only status/list/cancel remain send-only.

The canonical closed v2 projection binds the unchanged ordinary null-topic shell,
raw caption entities=[], explicit kind and closed per-kind stable facts. Documents
bind filename/MIME/positive known size/unique-ID SHA; static photos bind each
variant's type/dimensions/positive size/unique-ID SHA and sort by
(type,width,height,identity SHA), rejecting duplicate keys even with unequal size;
voice notes bind duration/audio/ogg/positive size/unique-ID SHA. SHA256 uses exact
`remote.unique_id` UTF8 bytes; this is provider media identity, not a remote byte
hash. Provider file IDs, remote.id, paths, download/upload counters, expected_size,
thumbnail/progressive detail, waveform and listened state are strictly typed and
bounded but excluded from stable facts. No mutable provider objects escape.

Main identities/sizes must be known and nonempty/positive. Caption bounds are
1024 Unicode characters and valid UTF8 without controls except newline/tab;
filename/MIME bounds are 255/127 UTF8 bytes. Photo vectors cap at 20 sizes and
100 progressive entries (0..2**31-1), dimensions 1..16384; unsafe flags, live video
and stickers refuse. Voice duration is 1..600 seconds, MIME exactly audio/ogg,
strict base64 waveform 0..100 decoded bytes and null/absent speech-recognition
result. MP3/M4A and longer voice targets remain unsupported. Thumbnail files may
have zero size; minithumbnails decode to at most 65536 bytes. Stored v2 JSON caps
at 65536 UTF8 bytes before parsing, validates exact canonical keys/types/variants
and checks display feasibility. Constructed projections are not authenticated;
fresh exact provider revalidation remains authoritative.

Render original strings through sanitation, then JSON escaping, with full facts
and explicit empty caption. The final marker-inclusive text is sanitizer-idempotent
and at most 4096 characters; overflow refuses instead of truncating. Raw facts
remain in the source SHA so sanitation cannot hide edits. The media text path
passes its existing guard to the shared pretransport boundary. A false result or
exception after observation registration discards only the unattempted observation
and raises MessageSendNotAttempted. Nothing at/after raw transport is classified
as local_failed. Existing claim, ownership, deadline, staging, correlation and
unknown/no-retry semantics remain authoritative. No target download/path opening
occurs. Synthetic evidence does not establish atomic remote edits, live delivery,
actual owner dialog or readiness of other consumers.


### Closed style-only text source projection

`reply_formatted_targets` is an independent default-off trusted capability for
both reply workflows. Private projection v3 retains the raw messageText shell
and canonical nonempty entities; v1 plain-text and v2 media canonical bytes stay
unchanged, and `is_media` includes v2, styled-caption v4 and lexical-caption v6. No public schema, tool annotation,
operation mapping, contract version or IPC version changes. Preparation,
inspection/update, the freshly read refresh source, approval, send/replay and pretransport
guards enforce every source-required capability. Gated v2/v3/v4/v5/v6 terminal replay checks
original provider instance/epoch as well as owner/account and monotonic retention
under the registry lock. Pending lifetime uses wall expiry; terminal retention
refuses at attempted_at+900 inclusive. Plain v1 replay behavior and read-only
send-status rules remain unchanged.

The exact allowed types are Bold, Italic, Underline, Strikethrough, Spoiler, Code,
Pre, PreCode, BlockQuote and ExpandableBlockQuote. Exact textEntity fields are
@type, offset, length and type; only PreCode's exact type has a language field.
Offsets/lengths are strict int32 values, never booleans, with nonnegative offset,
positive length and both original UTF-16 boundaries aligned to whole Unicode
scalars. Raw source is nonblank, at most 4096 Python characters, valid UTF8 and
has no category-C controls except newline/tab. Language has at most 64 characters
and 128 UTF8 bytes, with no controls. There are 1–32 entities; canonical order is
(offset, descending length, type, language). Duplicate identical spans/types,
crossings and unknown fields/types refuse. Code/Pre/PreCode are disjoint from all
other entities; block quote pairs are mutually disjoint. Adjacent spans and
ordinary style nesting/coextensiveness are allowed. These conservative exclusions
are a product bound. Semantics are pinned to td_api.tl SHA256
`326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87`,
lines 109–117 and 5716–5785; native execution/upgrade is unnecessary.

The immutable canonical projection caps at 65536 UTF8 bytes before parsing.
Constructor and target rendering independently validate the complete projection,
including shell, version, anchor and canonical entity order. The full marked
`Formatted target:` JSON record includes sanitized source, all styles, raw
UTF-16 offsets/lengths, sanitized exact covered substrings and optional sanitized
language. Its literal offset_basis is `original source UTF-16 code units`.
Sanitation never reassigns offsets; any source/language/span display change sets
sanitized. Every string is JSON-escaped and no HTML/Markdown style or hidden target
is interpreted. The entire marker-inclusive display is at most 4096 characters;
overflow refuses instead of truncating. V3 continues to refuse lexical types;
v5 below admits a separate bounded subset; v7 admits identity text and v9 admits
null-format DateTime text under their separate authority; exact fieldless Relative
text uses v11 and explicitly specified Absolute uses text v13 or caption v14 below.
Fieldless URL text uses v15 and supported URL media captions v16 below;
TextUrl messageText uses v23 below; TextUrl captions, custom emoji, media timestamps, unknown/open DateTime formats and embedded
reply/forward/import/markup remain unavailable.

Every revalidation compares the raw source digest, so range/language/quote-kind
changes fail closed. V3 text dispatch now passes the existing guard through the
shared pretransport boundary; staged artifact replies retain their immutable byte
checks and guards. A false/raising guard after registration discards only the
unattempted observation and records local_failed. Raw transport attempts remain
uncertain when confirmation is lost; late exact reducers may resolve them without
resend. Source styling never copies outgoing entities. Explicit text styles require
`formatted_text_send`; attachment captions stay plain. Topic, options, markup and
same-chat reply identity remain independently bound. Disable the capability or revert the candidate
change to roll back. Synthetic source/installed-process checks do not prove actual
Telegram delivery or broad consumer readiness.


### Styled media captions require conjunctive source authority

Private v4 combines the unchanged media projection with a nonempty styled caption.
V2 must have empty caption entities; v4 must have nonempty canonical entities.
Constructor and target independently check that boundary and canonical order.
The same closed ten style types, 32-span, original scalar-aligned UTF-16 and
overlap policies apply, with a nonblank caption bound of 1024 Unicode characters.
The complete marked JSON caption includes text, offset basis, style, raw
offset/length, covered text and optional language alongside all stable media facts.
Every displayed string contributes to the sanitation flag. Raw facts bind the
source hash before sanitation; full display caps at 4096 characters including
the marker and canonical source caps at 65536 UTF8 bytes. Overflow never truncates.

Source authority is an explicit tuple: plain (), media (reply_media_targets),
styled text (reply_formatted_targets), styled media (media then formatting), or
lexical text (reply_formatted_targets, reply_lexical_targets), or lexical media
(reply_media_targets, reply_formatted_targets, reply_lexical_targets).
Every production policy check requires every member. Legacy singular classifiers
refuse v4/v5/v6 rather than returning incomplete authority. The registry classifier
retains owner/account, pending wall expiry, attempted monotonic retention and
original provider/epoch checks. Stored inspection and update require the stored
source's complete tuple; a refresh requires the freshly read source's complete
tuple and, since the 0.54 candidate, no longer asks for the stored source it
replaces and never shows again; the new draft is built from the new source alone,
so revoked authority cannot be carried over. Revoking a source capability blocks preparation,
inspection/update, a refresh whose new source needs it, owner confirmation, final
transport guards and terminal replay. Send/status metadata rules and all public schemas remain
unchanged. No source attachment is downloaded or styling copied. Explicit text styles
require `formatted_text_send`; outgoing attachment caption entities remain empty.


### Lexical text source evidence requires both source opt-ins

Private v5 admits text only, with at least one exact fieldless Hashtag, Cashtag or
BotCommand provider entity and optionally the ten supported styles. Explicit
lexical parser mode is selected for these text sources and the separate v6 media
sources below; default style and media-caption parsing reject lexical types.
V1–v4 canonical/display bytes are
preserved. Constructor, target rendering and capability classification independently
validate the complete stored projection, version discriminator and canonical order.
The source requires (`reply_formatted_targets`, `reply_lexical_targets`), both
default-off; singular classification fails closed. V5 is not media; lexical
captions require the separate triple-authority v6 projection.

Exact closed shapes, nonboolean int32 coordinates, original scalar-aligned UTF-16
boundaries, canonical ordering, 1–32 entities and existing duplicate/crossing/code
exclusions apply. Lexical pairs and lexical/quote overlaps are additionally refused;
simple styles may nest or coincide with lexical spans in either direction. Raw
nonblank text caps at 4096 characters, canonical UTF8 caps at 65536 bytes before
JSON parsing, and complete marker-inclusive evidence caps at 4096 characters with
no truncation. The JSON labels `hashtag`, `cashtag` and `bot_command` accompany
covered text derived before sanitation. All displayed strings affect `sanitized`;
sanitation never changes source coordinates. These fieldless types follow pinned
td_api.tl lines 5722–5728; no independent lexical recognition grammar is invented.

Provider labels and source syntax remain untrusted evidence. They authorize no
execution, resolution, navigation or account selection. Raw shell/text/entities
bind the source digest; text, range, kind or metadata edits invalidate approval,
while entity permutation and irrelevant volatile fields do not. Every existing
plural lifecycle guard checks both capabilities, including the freshly read refresh source and
post-owner/register-before-wire boundaries. Owner/account, finite TTL and original
provider/epoch checks remain in force. False or raising pretransport guards discard
unattempted observations; unknown attempts retain uncertainty and exact late
reconciliation never resends. Source evidence never adds outgoing entities; text styles require
`formatted_text_send`, and attachment captions remain plain. Selected topics
require `topic_text_send`; TextUrl captions and custom emoji remain unsupported; TextUrl messageText uses v23 below; fieldless URL text uses v15 and supported URL captions v16 below; identity text uses v7 and null-format DateTime text uses
the separate v9 boundary below. Public schemas,
annotations, operation mappings, contract 1 and IPC 2 are unchanged; only the eight
reply-tool descriptions are intentionally corrected. Remove the lexical capability
or revert this local batch to roll back. Synthetic technical checks do not prove
live Telegram delivery or actual owner confirmation. Shipped skill source/caption
guidance is explicitly deferred to F18; complete consumer guidance is not claimed.


### Lexical media captions require all three source opt-ins

Private v6 extends only the existing closed document, static-photo and OGG
voice-note source projections with at least one exact fieldless Hashtag, Cashtag
or BotCommand entity, optionally mixed with the ten existing styles. V6 must have
a nonblank lexical caption; empty/styles-only entity lists fail the v6 boundary.
V2/v4 reject lexical entities. Text remains v5 and cannot be forged as v6. The
explicit lexical mode reuses the existing formatted validator; parser, validator
and renderer defaults remain style-only. V1–v5 canonical bytes, display bytes,
source authority, versions and bounds are preserved.

V6 requires (`reply_media_targets`, `reply_formatted_targets`,
`reply_lexical_targets`) conjunctively, beside send and the selected reply path.
All three source capabilities remain default-off. The source is media, and
singular source/registry accessors refuse to underreport its authority. Existing
plural guards enforce the exact tuple for preparation, pending get/update,
the freshly read refresh source, terminal replay, before/after owner decision, after fresh provider
properties and after observation registration before wire. Owner/account, wall
expiry, terminal monotonic retention and original provider/epoch checks are
unchanged. No guard or public operation/schema/IPC contract is broadened.

The raw caption has at most 1024 original Unicode scalars and 1–32 entities.
Exact fieldless type shapes, nonboolean int32 coordinates, scalar-aligned original
UTF-16 boundaries, canonical ordering and existing duplicate/crossing/overlap
exclusions remain in force. The complete marked Media target record contains all
sanitized caption text, every covered span with provider label and raw offset/
length, optional style language, and all stable media facts. Sanitation changes
are explicitly flagged; display coordinates never replace source coordinates.
Caption/span/media evidence is digest-bound before sanitation, never truncated.
Canonical projection caps at 65536 characters and UTF8 bytes before JSON parse;
complete marker-inclusive display caps at 4096 characters. Source file identity,
strict provider shape validation, volatile-field exclusions and media refusal
rules remain unchanged. Projection/render fetch no provider data, read no paths,
download no media, resolve no identities and execute no commands or links.

False/raising final guards discard only unattempted observations. Once raw
transport is attempted, uncertainty remains until exact reconciliation and
never authorizes resend. Explicit text styles require `formatted_text_send`; outgoing attachment
caption entities remain empty. Disable
reply_lexical_targets or revert the local batch to roll back. Technical synthetic
checks do not establish live delivery, actual owner clicks or consumer readiness;
the shipped consumer skill remains unchanged conservative F18 debt.


### Identity text source authority is explicit and conjunctive

Private v7 admits `messageText` with at least one exact provider identity entity:
fieldless `textEntityTypeMention`, or `textEntityTypeMentionName` with only `@type`
and `user_id`. The latter ID is a strict nonboolean positive int53 below 2**53;
strings, floats, missing/extra fields, zero and negative values refuse. V7 cannot
be forged from media or sources without identity entities. V1–v6 retain their
canonical bytes, rendering and authority; old version tags cannot authorize
identity entities. Identity media captions use the separate v8 boundary below.

Every v7 source requires (`reply_formatted_targets`, `reply_identity_targets`);
any lexical entity appends `reply_lexical_targets`. These capabilities remain
default-off and augment `send` plus the selected reply path. Existing plural
registry/Broker/provider guards enforce the exact conjunction on preparation,
get/update, the freshly read refresh source, before/after owner decision, after fresh source and
properties checks, after observation registration before wire, and terminal
replay. Singular authority accessors refuse conjunctive sources. Owner/account,
finite expiry, terminal retention and original provider/epoch checks are retained.

The closed parser preserves original UTF-16 offsets and lengths, exact type
metadata and canonical entity order. Nonboolean int32 coordinates must align
with original Unicode scalar boundaries. Identity spans exclude overlap with
other identity spans, lexical spans, code/pre and quotes; style nesting follows
existing exclusions. Duplicate/crossing spans refuse. The source has 1–32
entities and at most 4096 original Unicode scalars. Canonical projections cap at
65536 characters and UTF8 bytes before JSON parse; complete marker-inclusive
formatted evidence caps at 4096 characters. Overflow refuses before registry
mutation and never supplies a partial preview.

The complete escaped preview contains every identity span's label, original
coordinates and covered text; MentionName includes the declared `user_id`.
Sanitation changes are flagged but never alter digest-bound raw source facts.
Changing only `user_id` changes the source digest and refuses a stale send when
observed during final provider revalidation, including an edit during the local
owner decision. Provider evidence is cached/offline and is not an atomic remote
Telegram guarantee. Identity labels and IDs are untrusted assertions, never
authentication or recipient authority. No user resolution, navigation, media
download or source-directed send occurs. Source evidence never populates outgoing entities; explicit text styles require
`formatted_text_send`, and attachment caption entities stay empty. Denial and terminal/unknown replay never cause an extra send. Disable
reply_identity_targets or revert this local batch to roll back. Public schemas,
annotations, mappings, contract 1 and IPC 2 remain unchanged; only the eight reply
descriptions change. Synthetic checks do not prove live delivery, actual owner
clicks or consumer readiness; the installed/shipped consumer skill remains F18 debt.


### Identity media captions require the exact v8 source authority

Private v8 extends only the existing closed document, static-photo and OGG
voice-note source shapes. Its caption must contain at least one exact provider
Mention or MentionName entity. Parsing, stored validation and rendering require
explicit identity mode; old media v2/v4/v6 reject identity entities, and v8 rejects
empty, styles-only and lexical-only captions. V7 remains text-only. The same
closed formatted parser enforces positive nonboolean MentionName IDs below 2**53,
original UTF-16 scalar boundaries, canonical ordering, duplicate/crossing refusal
and conservative identity/lexical/code/quote overlap exclusions.

Every v8 source requires (`reply_media_targets`, `reply_formatted_targets`,
`reply_identity_targets`); exactly when any lexical entity is present,
`reply_lexical_targets` is appended. The default-off source capabilities augment
send and the chosen reply path. Existing plural server/registry/Broker/provider
guards retain their complete conjunction at preparation, get/update, the freshly
read refresh source, before/after owner decision, final provider checks, observation
registration before raw transport and terminal replay. No new capability or
operation is introduced. Singular authority accessors refuse conjunctive sources.

The complete marked Media target record shows caption text, every entity label,
original offset and length, covered text, declared user ID and stable media
metadata. Caption text remains bounded at 1024 original Unicode scalars, entities
at 32, canonical source at 65536 characters and UTF8 bytes, and the complete
marker-inclusive escaped display at 4096 characters. Sanitation is flagged and
never replaces raw digest-bound facts; overflow refuses before registry mutation.
No source retrieval performs identity resolution, navigation, media download,
path reads or read receipts. Explicit text styles require `formatted_text_send`; attachment caption entities
remain empty, and recipient/anchor selection remains unchanged.

Changing only user_id or stable media identity changes the source digest and
invalidates approval when observed at final source validation. A stale source
may still render the immutable stored owner preview before that final refusal.
The final source read and raw send are not atomic; cached/offline evidence is not
a remote Telegram guarantee. Owner denial or a policy refusal may leave a pending
draft; terminal and unknown replay never perform an extra send. Disable
reply_identity_targets or revert this local batch to roll back. V1–v7 canonical
bytes, displays and authority, all 43 schemas/annotations, operation mappings,
contract 1 and IPC 2 are preserved. Only the eight reply descriptions change.
Synthetic technical checks do not prove live delivery, actual owner clicks or
consumer readiness; the installed/shipped consumer skill remains F18 debt.


### DateTime text sources require exact v9, v11 or v13 source authority

The pinned `td_api.tl` SHA256 is
`326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87`.
At lines 5784–5785, `textEntityTypeDateTime` carries signed `unix_time:int32`
and nullable `formatting_type:DateTimeFormattingType`; null preserves original
text. Private v9 requires at least one DateTime and only absent/null formatting,
canonicalized to explicit null. V11 requires at least one exact fieldless Relative
constructor (line 5707) and cannot contain Absolute. Null-only text stays v9.

Private v13 is messageText-only and requires at least one validated
`dateTimeFormattingTypeAbsolute`. Its exact declaration at line 5713 has
`time_precision:DateTimePartPrecision`, `date_precision:DateTimePartPrecision`
and `show_day_of_week:Bool`. Each precision must be an exact fieldless None,
Short or Long constructor (full names `dateTimePartPrecisionNone`,
`dateTimePartPrecisionShort`, `dateTimePartPrecisionLong`, lines 5695/5698/5701).
The matching generated header SHA256 is
`8285128040cace9611f84abe6ff517bb0a14d35004a557a78c3cf001c58ebae5`;
lines 8406–8414 declare two object pointers and one boolean. There is no format
string and no invented string grammar. All 18 explicit combinations are admitted,
including both None with false. Strict nonboolean signed-int32 timestamps,
closed dictionaries and an explicit strict weekday boolean are required.

Both precision objects must be explicitly present and non-null in this
conservative product subset. This is not a TDLib validity claim: JSON parser
infrastructure can accept missing/null pointers and coerced scalars, but the
schema does not define null precision rendering, boolean defaults or equivalence
to the explicit None constructor. Missing/null precision, missing/null/coerced
weekday values, unknown/open constructors and extra fields refuse. V13 may mix
null, Relative and Absolute DateTimes with existing permitted adjacent entities.
The original text, timestamp and formatting fields remain opaque provider facts.

V1–v8 cannot authorize DateTime; v9 requires null DateTime; v11 requires Relative
without Absolute; v13 requires Absolute. Canonical reconstruction rejects forged
version labels and v13 without Absolute. Existing v1–v12 canonical bytes and
authority remain unchanged. DateTime captions use the separate v10/v12/v14
boundary below. The shared formatter remains null-only by default; Relative and
Absolute have separate internal opt-ins. Stored v13 text and v14 media explicitly
enable Absolute; legacy stored versions cannot acquire it through the formatter.

Every DateTime text source requires `reply_formatted_targets` and default-off
`reply_datetime_targets`, adding `reply_lexical_targets` exactly when lexical
entities coexist and `reply_identity_targets` exactly when identity entities
coexist. Existing plural source-policy guards enforce that conjunction during
preparation, inspection/update, the freshly read refresh source, owner decision, final source
and properties checks, observation registration before raw transport and terminal
replay. Send/reply-path, owner/account, finite retention and original provider/epoch
constraints remain in force. Status/list/cancel retain send-only authority.
No public operation, schema, capability or annotation is added or enabled.

DateTime is exclusive of every overlapping entity; adjacent spans remain eligible.
Existing 1–32 entities, 4096 original scalars, whole-scalar UTF-16 coordinates,
65536-character/UTF8-byte canonical source and complete marker-inclusive
4096-character preview bounds still apply. Overflow refuses before registry
mutation without truncation. The escaped preview shows each DateTime span's
`type="date_time"`, `provider_type="textEntityTypeDateTime"`, signed `unix_time`,
exact null/Relative/Absolute `formatting_type`, offset/length and covered text.
Nested precision constructor names and the weekday boolean are complete evidence.
Sanitation is flagged without changing raw source identity or coordinates.

No relative or absolute date text is synthesized; no clock/locale lookup,
timezone inference, timestamp-to-text agreement claim, conversion or scheduled
action occurs. A timestamp, precision, weekday or constructor change alters the
source and review digests. Update preserves the exact source without rereading;
refresh rebuilds it under the current source version, invalidates the old handle
and requires fresh approval. An edited source may still reach the stored owner
preview; final source validation after owner rendering refuses drift before raw
send. The final read/send interval remains non-atomic. Cached/offline provider
evidence is not an atomic remote Telegram guarantee. Source evidence never populates outgoing entities; optional text styles require
`formatted_text_send`, and attachment captions stay plain;
denial, policy refusal, receipts and unknown outcomes never permit automatic resend.

Disable `reply_datetime_targets` or revert this local batch to roll back. V1–v12
canonical bytes, displays and authority, all 43 public schemas/annotations,
contract 1, IPC 2 and schema fingerprint are preserved. Eight relevant tool
descriptions explain the new source subset. Synthetic checks do not prove live
delivery, actual owner clicks or consumer readiness; the installed/shipped skill
remains separate F18 debt and is unchanged.

### DateTime captions require exact private v10, v12 or v14 authority

The existing bounded document/static-photo/OGG-voice-note projections admit
null-only DateTime captions through v10. Private v12 requires at least one exact
fieldless Relative and no Absolute. Private v14 requires at least one validated
Absolute with exactly the explicit precision objects and strict weekday boolean
described above; all 18 combinations are admitted. Null, Relative and Absolute may
coexist in v14. This explicit-field subset is a conservative local admission policy,
not a claim that TDLib rejects omitted/null precision fields. Unknown/open fields,
missing/null precision and missing/null/coerced weekday refuse.

The media parser opts into Absolute only for raw admission; persisted v14 explicitly
enables it during validation/rendering. Stored v2/v4/v6/v8 keep DateTime disabled;
v10 keeps Relative and Absolute disabled; v12 keeps Absolute disabled. Presence
checks run after full entity validation. Stored v12 without Relative, v14 without
Absolute, Relative captions relabeled v1–v11, Absolute captions relabeled v1–v13,
noncanonical entities and unsafe media facts refuse. V1–v13 canonical bytes,
display and authority remain unchanged, including v13 messageText.

V10, v12 and v14 require `reply_media_targets`, `reply_formatted_targets` and
`reply_datetime_targets`, with lexical/identity authority exactly when present.
No capability is enabled by this update. The full escaped caption span record,
provider timestamp/type, exact null/Relative/Absolute constructor, both precision
constructors, weekday boolean and stable media facts enter the immutable source
and preview digests. Strict signed-int32 timestamps, original whole-scalar UTF-16
coordinates and every DateTime overlap exclusion remain in force. Existing 1024
caption scalar, 32 entity, 65536 canonical character/UTF8-byte and complete
marker-inclusive 4096 display bounds refuse overflow without truncation.
No source media is downloaded; volatile local paths and provider file IDs stay
outside the stable evidence.

Checks at preparation, inspection/update, the freshly read refresh source, owner rendering, final
provider read, pretransport registration and terminal replay retain the complete
capability conjunction and existing refusal semantics. Update keeps the source
snapshot without rereading. Refresh rebuilds it, including v14/v12/v10 transitions;
new handles require new approval. Nested precision, weekday, constructor, caption
or stable media identity changes invalidate the stored approval. Explicit text styles require `formatted_text_send`; outgoing attachment
captions retain empty entities. No clock, locale or timezone interpretation, date
conversion, identity resolution or source-triggered work occurs. The final source
revalidation/send interval remains non-atomic, and an uncertain transport outcome
is never retried. Actual owner/provider delivery requires its own concrete
approval; synthetic installed-consumer evidence proves only the reported technical
scenario. Public schemas/annotations, 43 tools (at that slice), contract1 and IPC2 stay unchanged;
the consumer skill remains unchanged and its F18 validation is outstanding.

### Fieldless URL text and media captions require private v15/v16 source authority

The pinned declaration's `textEntityTypeUrl` is fieldless. Admission is limited
to an exact `{ "@type": "textEntityTypeUrl" }` in `messageText`, selecting private
v15 only when at least one Url has passed complete shape, UTF-16 and overlap
validation. Supported document, static-photo and OGG-voice-note captions select
private v16 only after complete validation with at least one exact fieldless Url.
The low-level formatter and media parser/validator/renderer keep `url=False` by
default; only the validated URL projection selects that mode.
TextUrl messageText requires separate v23 authority below; its captions,
CustomEmoji and MediaTimestamp remain closed. BankCardNumber messageText uses
v21; supported captions use v22 with media authority. EmailAddress captions require separate
v18 authority; v17 messageText authority is described below. Legacy v1–v14 tags cannot authorize
Url; v15 cannot authorize URL captions. V15/v16 without Url refuse. Earlier
v1–v15 canonical/display bytes and authority remain unchanged.

Every v15 source requires (`reply_formatted_targets`, `reply_url_targets`)
conjunctively. V16 additionally requires `reply_media_targets`. All three are
default-off trusted capabilities. DateTime, lexical and
identity capabilities are appended exactly when their validated entities
coexist. The singular capability accessor refuses conjunctions. Existing plural
registry, Broker and provider guards enforce the complete authority at prepare,
get/update, the freshly read refresh source, owner decision, final source/properties checks,
before wire and terminal replay. No known capability, public tool, schema,
annotation, operation mapping or IPC field is added by v16.

Raw source text/caption and stable media evidence stay immutable. Complete escaped preview records include
`provider_type="textEntityTypeUrl"`, `type="url"`, original whole-scalar UTF-16
offset/length and the entire sanitized covered substring, beside full sanitized
text. Sanitation is display-only; no grammar, normalization, navigation, URL
fetching, reputation inference, resolution or link preview occurs. Url is
locally exclusive with lexical/identity/Url, quote/code and DateTime spans.
Ordinary style containment is permitted; crossing/duplicate/invalid spans refuse.
This is a conservative local subset, not a TDLib validity claim. Existing
4096 text-source-scalar, 1024 caption-scalar, 32-entity, 65536 canonical
character/UTF8-byte and complete
marker-inclusive 4096 display bounds refuse overflow without truncation.

Update preserves source evidence without rereading it. Refresh binds a new
snapshot and invalidates old IDs/approval, including transitions between v15/v16
and older supported sources. No source media is downloaded. Authority already absent or revoked in the owner
callback refuses before claim and staging. Later revocation at final properties
or immediate prewire guards blocks raw transport after claim and, for artifact
replies, temporary staging; the draft terminal-fails and staged data is retired.
Original source text/caption/span/type or stable media drift after owner decision also refuses before raw
transport and may follow claim/staging. There is no stronger atomicity or
pre-staging guarantee for these later checks. Caller false and owner denial do
not send/stage. Uncertain outcomes never authorize resend. Source evidence never adds outgoing styles. Explicit text styles require
`formatted_text_send`; attachment captions keep empty entities. Both routes
preserve recipient/anchor/options;
source labels never trigger work.

Disable `reply_url_targets` or revert the isolated batch for rollback. Technical
synthetic checks do not establish native TDLib/live Telegram delivery, actual
owner clicks or consumer readiness. Product completion and the unchanged
consumer skill's F18 validation remain outstanding.


### Fieldless EmailAddress messageText/v18 media captions require distinct authority

The pinned `textEntityTypeEmailAddress` declaration is fieldless. Only the exact
`{"@type":"textEntityTypeEmailAddress"}` is admitted in messageText; v17 is
selected only after full shape, original whole-scalar UTF-16 and overlap
validation with at least one EmailAddress. The four formatter/parser/rendering
entry points retain internal `email=False`. Supported document/static-photo/
OGG-voice-note captions select private v18 only after complete valid caption
and media validation containing EmailAddress. Media parser/validator/renderer
also retain default `email=False`, even with other family opt-ins. V18 requires
`reply_media_targets`, `reply_formatted_targets` and `reply_email_targets`,
then URL, DateTime, lexical and identity authorities exactly when present.
TextUrl messageText requires separate v23 authority below; its captions,
CustomEmoji and MediaTimestamp remain closed. BankCardNumber messageText uses
v21; supported captions use v22 with media authority.
PhoneNumber messageText requires separate v19 authority; Supported PhoneNumber captions require separate v20 authority below. Forged older email-caption tags and email-less v17/v18 refuse; v17
remains messageText only. V1–v17 canonical/display bytes and authority remain
unchanged. Raw captions and stable media identities remain immutable; no
source download occurs.

Every v17 source requires both default-off `reply_formatted_targets` and
`reply_email_targets`. URL, DateTime, lexical and identity authorities are added
exactly when their validated entities coexist. Existing plural registry, broker
and provider guards enforce the conjunction at preparation, stored inspection
and update, the freshly read refresh source, owner/final source/properties checks,
immediate prewire and terminal replay. V18 reuses the existing email capability;
public input/output schemas, annotations, operation mappings, contract 1, IPC 2
and the schema fingerprint are unchanged. Tool descriptions are outside that
fingerprint and describe the new source evidence.

The projection preserves original complete raw text and scalar-valid UTF-16
coordinates; the escaped complete preview shows `type="email_address"`,
`provider_type="textEntityTypeEmailAddress"`, offset/length and every full
sanitized covered substring alongside full sanitized text. Sanitation is
display-only and never supplies source coordinates or identity. Provider labels
and covered text are inert untrusted assertions, with no email grammar,
deliverability inference, address resolution, mailto/navigation, contact action
or email sending. EmailAddress locally excludes overlap with other exclusive
families (URL, lexical, identity, EmailAddress), DateTime, quotes and code;
ordinary style containment remains allowed. Crossing/duplicate/malformed,
extra-field and split-surrogate entities refuse. These are conservative local
constraints, not claims of TDLib semantics. Unchanged 4096 text/1024 caption-scalar,
32-entity, 65536 projection character/UTF8-byte and complete marker-inclusive
4096 display bounds refuse overflow without truncation.

Get/update retain the immutable stored source without provider reread; refresh
requires the newly read source's conjunction and invalidates old IDs and approvals. Missing
authority or owner-callback revocation refuses before claim/staging. Later
properties/prewire revocation or source drift can follow claim and transient
artifact staging, but terminal-fails, retires staging and blocks raw transport.
There is no stronger atomicity or pre-staging guarantee. Explicit text styles require `formatted_text_send`; attachment captions stay
plain with empty entities. Both reply routes preserve recipient/anchor/options; source labels
never cause an email action. Disable `reply_email_targets` or revert the local
batch for rollback. Synthetic evidence does not establish live Telegram, actual
owner clicks or consumer readiness; F18 and the unchanged consumer skill remain
outstanding.


### Fieldless PhoneNumber messageText requires separate default-off authority

Exact fieldless `textEntityTypePhoneNumber` selects private v19 only after
complete validated messageText contains PhoneNumber. Internal formatter,
validator and renderer entry points retain `phone=False`. V19 requires
`reply_formatted_targets` and `reply_phone_targets`, adding
`reply_email_targets`, `reply_url_targets`, `reply_datetime_targets`,
`reply_lexical_targets` and `reply_identity_targets` exactly when present, in
that order. The singular accessor refuses conjunctive authority. TextUrl
messageText requires separate v23 authority below; its captions, CustomEmoji
and MediaTimestamp are closed. BankCardNumber messageText uses v21;
supported captions use v22 with media authority.

Supported document/static-photo/OGG-voice-note PhoneNumber captions select
private v20 only after complete caption/media validation containing exact
fieldless PhoneNumber. They additionally require `reply_media_targets`; all
conditional families keep the same order. Media parser/validator/renderer
retain `phone=False`. Complete escaped caption spans, raw caption and stable
media identity bind the immutable preview digest. No source download occurs.
Forged older tags and phone-less v19/v20 refuse; v19 remains messageText only.
V1–v19 canonical/display bytes and
authority, public schemas/annotations/mappings, contract1, IPC2 and fingerprint
remain unchanged. The consumer skill remains unchanged.

The complete escaped preview preserves `type="phone_number"`,
`provider_type="textEntityTypePhoneNumber"`, original whole-scalar UTF-16
offset/length, complete covered literal and full source text. Raw text and
provider facts bind immutable source identity and review digest; sanitation is
display-only. Provider labels are inert untrusted assertions; no number grammar,
E164 validation, normalization, country inference, tel/navigation, dialing,
resolution or contact creation occurs. Existing conservative overlap rules
extend to phone versus semantic families, DateTime, quotes and code, while
ordinary style containment is supported. Duplicate/crossing/malformed,
extra-field and split-surrogate entities refuse. These local restrictions do
not assert TDLib validity. Message4096/caption1024 scalars,32entities,
projection65536 characters/UTF8 bytes and complete display4096 bounds are
unchanged; incomplete/truncated evidence never becomes a draft.

Existing plural registry, broker and provider guards preserve all source
capabilities through preparation, immutable get/update, the freshly read refresh source,
owner/final source/properties checks, immediate prewire and terminal replay.
Missing authority or owner-callback revocation refuses before claim/staging.
Late properties/prewire revocation or source drift may claim and transiently
stage an artifact, but refuses raw transport, terminal-fails and retires
staging. No atomic or stronger pre-staging guarantee is claimed. Explicitly approved text styles require `formatted_text_send`; outgoing
attachment captions retain empty entities. Both routes preserve exact recipient,
anchor and options; source assertions cause no phone action. Disable
`reply_phone_targets` or revert the local batch for rollback. Synthetic
transport/owner evidence does not establish live Telegram delivery, actual
owner clicks, product outcome or F18 readiness.

### Fieldless BankCardNumber messageText requires separate default-off authority

Exact fieldless `textEntityTypeBankCardNumber` selects private v21 only after
complete shape, original whole-scalar UTF-16 and overlap validation containing
BankCardNumber in `messageText`. Formatter/validator/renderer retain
`bank_card=False`. V21 conjunctively requires `reply_formatted_targets` and
`reply_bank_card_targets`, followed by `reply_phone_targets`,
`reply_email_targets`, `reply_url_targets`, `reply_datetime_targets`,
`reply_lexical_targets` and `reply_identity_targets` exactly when present. The
singular accessor refuses. Supported document/static-photo/OGG-voice-note
BankCardNumber captions select private v22 only after complete caption/media
validation containing BankCardNumber, additionally requiring
`reply_media_targets`; caption helpers retain `bank_card=False`. TextUrl
messageText uses v23 below; its captions, CustomEmoji and MediaTimestamp remain unavailable.

Complete escaped evidence preserves `type="bank_card_number"`,
`provider_type="textEntityTypeBankCardNumber"`, original UTF-16 offset/length,
covered literal and complete source text. Immutable raw text and provider facts
bind source identity and review digest; sanitation is display-only. Non-numeric
literals are accepted as inert provider assertions. No grammar/Luhn/PAN check,
normalization, `getBankCardInfo`, payment, links, navigation, contact/action
resolution or source download is performed. Conservative exclusivity covers
BankCardNumber versus semantic families, DateTime, quotes and code; ordinary
style containment remains allowed. Duplicate/crossing/malformed/extra-field
and split-surrogate entities refuse. Message4096/caption1024 scalars,32entities,
projection65536 characters/UTF8 bytes and complete display4096 bounds remain;
overflow refuses without truncation.

Existing plural guards enforce every present family through preparation,
immutable get/update, the freshly read refresh source, owner confirmation, final
source/properties checks, immediate prewire and terminal replay. Missing
authority or owner-callback revocation refuses before claim/staging. Later
properties/prewire revocation or source drift may claim and transiently stage
an artifact, but refuses raw transport, terminal-fails and retires staging.
No stronger atomicity is claimed. Update never rereads source; refresh binds
new evidence and invalidates prior IDs and approval. Forged old tags,
BankCardNumber-less v21 and media-as-v21 refuse. Private v1–v20 canonical/display
bytes and authority, public schemas/annotations/mappings, contract1, IPC2 and
fingerprint remain unchanged; the consumer skill is unchanged. Explicit text styles require `formatted_text_send`; attachment captions retain
empty entities. Both routes preserve exact recipient, reply anchor and options. Disable `reply_bank_card_targets` or revert this local batch for
rollback. Synthetic evidence does not establish native/live delivery, actual
owner clicks, product outcome or F18 consumer readiness.

### TextUrl messageText requires separate default-off v23 authority

Only complete validated messageText containing exact
`{"@type":"textEntityTypeTextUrl","url":<string>}` selects private v23.
Formatter/validator/renderer entry points retain `text_url=False`; media helpers
do not opt in. TextUrl captions and forged older/newer tags, v23 without TextUrl
and media-as-v23 refuse. The hidden field must be an exact built-in string of
zero through 4096 Unicode scalars that encodes as strict UTF-8. This is a local
allocation bound, not a provider URL limit. Empty, whitespace, C0/C1/bidi,
arbitrary schemes, markup and literal escapes remain opaque data; no scheme
validation, strip, normalization, decoding, resolution, navigation, preview
fetch or source download occurs. Visible text keeps its prior nonblank/control
policy. TextUrl joins the conservative semantic overlap family; duplicates,
crossings, semantic/DateTime/code/quote overlap and split-surrogate ranges refuse.
Ordinary style containment/coextensiveness remains allowed.

V23 requires `reply_formatted_targets`, `reply_text_url_targets`, then each
present `reply_bank_card_targets`, `reply_phone_targets`, `reply_email_targets`,
`reply_url_targets`, `reply_datetime_targets`, `reply_lexical_targets` and
`reply_identity_targets`, in that order. TextUrl alone does not require fieldless
URL authority; the singular accessor refuses the conjunction. V23 display uses
one reversible JSON layer: fixed keys retain normal encoding; only ASCII letters,
digits, underscore and hyphen remain literal in string values. All other scalars
use lowercase four-hex Unicode escapes, paired for astral scalars. Full original
text, covered text, provider metadata and hidden `provider_url` survive parsing
that single layer exactly. No renderer/action unescapes them. `sanitized` compares
encoded and ordinary JSON literals of original provider strings, excluding fixed
labels. Complete marker-inclusive display4096, 32entities and projection65536
characters/UTF-8 bytes remain; overflow refuses without truncation.

Exact hidden-only substitution changes source identity and preview digest.
Existing plural guards cover preparation, get/update, the freshly read refresh source's authority,
owner return, properties, immediate prewire and terminal replay. Missing/owner-
revoked authority refuses before claim/staging; later revocation/drift may claim
and transiently stage, then local-fail before raw transport. Retirement is best
effort with TTL fallback; uncertain transport never retries. Private v1–v22 bytes
and authority, public43tools, contract1, IPC2 and schema fingerprint remain
unchanged; known capabilities total27. Explicit text styles require `formatted_text_send`; attachment captions retain
empty entities. Both routes preserve exact approved options. Disable `reply_text_url_targets` for rollback.
Synthetic evidence does not close native/live provider, actual owner-dialog,
F18 outcome, license or public release gates.

## Exact forum-topic text sending (0.49.0)

`topic_text_send` is a new default-off capability required together with `send`.
Preparation accepts only a strict typed forum selector. Immutable topic evidence
binds account, chat, native chat type/peer, topic ID, complete marked name plus
original-name hash, creator, creation time and current state. Closed/hidden or
incomplete topics are rejected. The complete preview digest also binds the
draft ID, recipient title, normalized text/hash/bytes, expiry and exact fixed
wire options. Ordinary no-topic draft projections retain their existing shape.

Account, provider instance/session, policy, cancellation/claim and expiry are
checked around owner approval and immediately before transport; native topic
metadata is rehydrated under one aggregate deadline. Registration alone does
not authorize transport. Final and recovered observations require exact topic,
chat, correlation IDs and plain text. The final response and receipt/status
disclosure recheck current authority; an unavailable check never authorizes a
retry. Original draft expiry does not erase a retained attempted-send receipt.
No caller can replace topic/digest on apply. Text revisions supersede the old
draft; topic refresh requires a new preparation. The new optional public fields
change the schema fingerprint; capabilities now total28. This is technical
scope only, with live delivery and native consumer acceptance still separate.


## Exact forum-topic text replies (0.50.0)

The existing reply preparation accepts an optional strict forum reference. A topic
reply requires the conjunction of `send`, `reply_text_send`, `topic_text_send`
and all capabilities required by its source. No capability becomes a default.
The provider verifies current topic metadata, exact typed source membership and
reply eligibility under the inherited aggregate deadline. Missing or mismatched
topic evidence never falls back to an ordinary reply or a different topic.

The complete topic snapshot and source snapshot share one outer version2 reply
review digest. Topic text content has no independent inner preview digest.
Ordinary replies retain their version1 bytes and digest. Source canonical versions
and their authority requirements remain unchanged. Outgoing artifact replies
reject a topic field even when an attacker recomputes their ordinary digest.

The registry retains the exact provider and session epoch with both snapshots.
Preparation, inspection, revision, owner confirmation, claim and transport require
current authority. Text revision checks local authority before replacing the old
entry under the registry lock; refusal leaves the old pending entry unchanged.
Topic reply refresh is unavailable. Final locked revalidation occurs before and
after registration, immediately before a single raw transport attempt. A failed
pretransport guard discards the unused registration; uncertainty after an attempt
retains the ordinary 900-second observation window and never authorizes retry.

Pending, recovered, final and late observations require both exact topic and reply
identity plus approved plain text. Unsupported media/quote/options remain denied.
The public optional input/evidence changes the schema fingerprint but adds no tool
or capability:43 tools and28 capabilities (at that slice). Installed synthetic evidence remains
separate from actual owner-dialog, live delivery, native consumer and release gates.
## Unreleased bundled first-install boundary

Version 0.52.0 authenticates a bounded USTAR/gzip index and every
member using an independently pinned prebuilt native seed before extraction or
Python execution. Archive links, traversal, duplicates, extension headers,
special files and unexpected entries refuse. Builder-only internal Python
links are audited to remain inside the supplied Python root and are dereferenced
to ordinary files in the archive. The consumer invokes no compiler or pip.

Installed launchers admit an owner-only, no-follow/ACL-checked release tree and
receipt/index binding before app imports. Native selection comes from this
local launcher context; environment variables and MCP input never choose a
library. The default session, credential store, shared owner lease, enrollment
and strict auth handoff remain the same. LaunchAgent validation permits only
the exact receipt-bound isolated form alongside the existing four-argument
legacy form. There is no PYTHONPATH or DYLD propagation.

The recoverable installation transaction owns a separate runtime root, one
lease/journal, matched skills and its Codex subtree. It also adds, only when absent and
at most once per installation, one Claude Code user-scope `telegram_search` entry: state is
read from the `mcpServers` object of `~/.claude.json` (bounded, no-follow, strict JSON, no
process started), a different existing entry refuses the install before anything is created,
and the entry is written only by the `claude` CLI found at four fixed paths (never `PATH`),
run as an argument list without a shell, with a minimal environment and discarded output, then
confirmed by re-reading the file. Skills are copied to `~/.claude/skills` only when `~/.claude`
exists or the CLI was found. Original config bytes stay
in private transaction evidence and are never logged. Writes compare original
bytes immediately before atomic replacement. These cooperative checks detect
observed drift; they do not protect against a malicious process already
controlling the same OS UID or make arbitrary editors participate in the lease.
Initialization uses an inode-bound durable intent before root publication.
Release, skills and local metadata retain staged-object publication records;
pending recovery requires that exact object, not merely matching content. The
owned Codex subtree permits unrelated neighbor edits, after its completed publication and while a
transaction runs: its ownership hash covers only the table's own keys and values, not the comments and
blank lines tomlkit attaches after its last key (a comment above the next table), and a replacement or
removal splices only the table's own lines, located exactly, so every other byte stays. A config that
changed after the transaction recorded its preimage is written over its current bytes while the
`telegram_search` entry is still the preimage's (or was removed); any other entry refuses, even one equal
to ours, because only the object the transaction wrote is ever taken as its own. Interrupted unbound packets are preserved without adoption, and
recovery attempts are bounded.
Current pointer publication occurs last; interrupted installations fail closed
until exact checked recovery. A first install stops no active broker and adopts no foreign data.
Upgrade, rollback, uninstall and the explicit `--adopt-legacy` option of the 0.53 candidate are the
exceptions, with their own gates (see
[the upgrade, rollback and uninstall invariants](#upgrade-rollback-and-uninstall-invariants-053-candidate)).


## QR connect boundary (0.52 candidate)

`connect`, `wait` and `cancel` take no secrets and print fixed JSON; none prints the page address
or its token. `connect` starts a transient per-user launchd job
(`com.<home-directory-name>.telegram-search-mcp.connect`: a plist in the private `run` directory,
mode `0600`, `KeepAlive` false, Aqua session, output to `/dev/null`) that runs the internal
`connect-helper`; the helper refuses to run unless its launchd service name is that label. In
native Terminal the helper runs inline instead. A dedicated `run/connect.lock`, separate from the
session owner lease, keeps exactly one helper alive. The run takes the owner lease for the whole
authorization and releases it before the broker handoff. It writes only `run/connect-status.json`
(mode `0600`, at most 1 KiB: `phase`, `code`, `updated_at`, `expires_at`, `retry_after`, all from
closed vocabularies); `wait` reads it strictly and reports only what it validates.

The helper serves a stdlib HTTP screen on `127.0.0.1` at an ephemeral port. A fresh random 32-byte
token lives only in the URL fragment, so it is never sent in a request or referrer; the page
returns it as a bearer token that the server compares in constant time. The static page assets
are served without it; `/api/state`, `/api/password` and `/api/cancel` require it. Every request
must carry exactly the `Host` `127.0.0.1:<port>`; a POST must also carry exactly the matching
`Origin` and a JSON body of at most 4 KiB. Other paths, methods and oversized bodies are refused
with empty bodies, and there are no CORS headers. Responses set a strict Content-Security-Policy
(`default-src 'none'`, scripts and styles from the page's own origin), `no-store`, `no-referrer`,
`nosniff` and frame/cross-origin denial. The server allows 16 concurrent connections, drops a
silent client after 10 seconds, cuts a connection off after 15 seconds and disables port sharing.
Nothing is written to stdout, stderr or any log. The default browser is opened through
`osascript` with the script on stdin, after the URL is checked against the server's exact shape,
so the token is never in any process's argv. If no browser opens, the run continues and `wait`
keeps reporting its phase until `cancel` or expiry.

The QR link and the 2FA password exist only as `SecretValue`s. The link (at most 256 bytes) is
rendered server-side to an SVG data URI and not kept. The password travels from the POST body to
the run thread and is never stored, echoed, logged or placed in the status file, a result or an
exception. At most 5 password submissions are accepted per run with one request in flight; only a
definitive `invalid_password` or `invalid_input` lets the page ask again, a timeout or transport
error stops as `password_submission_uncertain` without retry, and a password is not sent with
less than 5 seconds of the run left.

The whole run lives 600 seconds on the wall clock, so a sleeping Mac does not extend it. After a
terminal phase the page stays up 10 seconds, the server closes and the helper removes its own
plist. `cancel` boots out only this job; the helper's SIGTERM handler cancels the run through the
page so the temporary auth client is closed and destroyed normally and the status becomes
`cancelled`. The page address, token fragment included, may remain in the browser's history; the
token is valid for that one run only (at most 600 seconds) and the server is closed afterwards, so
the stored address opens nothing later.

`connect` never stops a foreign, incompatible or Ready owner. The one exception is an enrolled,
receipt-bound broker that answers a compatible handshake but whose Telegram authorization is not
ready: after read-only validation it may be stopped, the QR flow runs on the existing managed
enrollment and key, and the broker is put back whenever no helper reaches Ready. A compatible
broker whose first check outlives 25 seconds is reported as `broker_starting` and nothing is
stopped.

A new enrollment uses a complete API pair already in the Keychain, else the hash-authenticated
`app-identity.json` bundled in the release (present only when the bundle producer was asked with
`--app-identity-from-keychain`), else refuses `app_identity_missing`. The enrollment binds that
source and a SHA-256 fingerprint of the pair; a changed identity refuses `identity_changed`. A
partial Keychain pair is `credential_conflict`. Every user of a bundle shares the distributor's
API ID, and distributed client parameters are extractable by design; the owner accepted this on
2026-10-14, which is not legal sign-off. The owner's own pair (`api-credentials`, native Terminal
only, add-only, before the first `connect`) overrides it. A new policy lists only `read` and
`artifacts`. The loopback checks protect against other websites and DNS rebinding; they do not
claim protection against hostile software running as the same OS user. Evidence is synthetic
(fake Telegram, launchd, `claude` CLI and Keychain); real clean-user acceptance is pending.


## Claude Desktop registration boundary (0.53 candidate)

The installer can add, replace or remove exactly one key, `mcpServers.telegram_search`, in
`~/Library/Application Support/Claude/claude_desktop_config.json`. When the `Claude` directory does
not exist it reads and writes nothing. It starts no process and reads no other Claude Desktop file.

**Read.** The file is read through held no-follow parents and a descriptor, at most 1 MiB, with ACL
checks. It must be owned by you, have one link and no group or world write bit. Mode `0644`, which
Claude Desktop writes, is accepted; a file the installer creates is `0600`. The bytes must be strict
JSON: one object, no duplicate key, no `NaN` or `Infinity`, at most 64 levels of nesting. An unsafe or
unparsable file reads as `unavailable`: the install continues without Claude Desktop and returns a
manual entry.

**Write.** A write replaces only the exact bytes just observed. The temporary file's name is journaled
before it is created and its inode before the replace; the file is compared again through the same held
directory immediately before an atomic replace, and the replaced file's mode is kept. The candidate is
re-read and compared with the original: every neighbouring server and value must keep its meaning and
every number its exact spelling, or nothing is written and the install refuses
(`claude_desktop_config_unsupported`). The config can hold other servers' secrets, so its bytes are never
logged, echoed or raised; only hashes leave the module. The one exception is an adopted entry's own
backup, which stays in the installation's private transaction evidence.

**Ownership.** The receipt (`claude_desktop_ownership`) records an entry only when this installer wrote
it: a first install, upgrade or rollback whose step ended `registered`. An upgrade or rollback whose
step ended otherwise replaced nothing and keeps the earlier record, so the entry it left in place stays
owned; ownership is derived from the transaction journals, so a v1 receipt is read the same way. A first
install adds only an absent entry. An identical entry is `reused` and not owned. A different
entry refuses the whole install before anything is created (`claude_desktop_registration_conflict`);
only `--adopt-legacy` replaces one, after its exact bytes are backed up. An upgrade or rollback replaces
the entry only while it still equals the owned one, and an uninstall removes it only while it is exactly
that, so an entry you changed is kept. After an interruption the write is made again, planned over the
file as it is then, only while our entry is still absent, the owned one or the adopted one, at most 8
attempts, so a neighbour written meanwhile is kept; a killed attempt's temporary is unlinked only while it
is the journaled inode.

**Limits.** Claude Desktop may rewrite its configuration from memory while it runs; the read-only
`status` action reports `drift` and the manual entry, and the install asks for a restart. The entry
names an exact release path. These cooperative checks detect drift; they do not protect against a hostile
process running as the same OS user.

## Claude Code plugin boundary (0.53 candidate)

The plugin ships skills, a marketplace entry and an MCP server; it grants no capability. The broker's
`runtime.toml` still decides what is allowed, and the policy-filtered `tools/list` only hides what the
policy does not enable.

**The shim.** `.mcp.json` runs `/bin/sh -p ${CLAUDE_PLUGIN_ROOT}/bin/mcp-entry`. The shim takes no
argument and accepts no environment value as input: the home directory comes from the passwd entry
(`id -P`) and must be an absolute path without `=`, quote, backslash or non-printable byte. It then
requires `<home>/Library/Application Support/TelegramSearch/current.json` to be a regular file, not a
link, owned by you, with one link, at most 4,096 bytes and no group or world write bit. The pointer
must be exactly the installer's bytes: `schema_version` the integer 1, `index_sha256` 64 lowercase hex
digits, `release_root` equal to `<root>/releases/<index_sha256>`, sorted keys, no spaces and one
newline, so a repeated or extra key cannot hide behind what `plutil` extracts. The release directory
(not a link), its `python/bin/python3` (executable) and `launcher.py` must exist. Then it runs
`exec /usr/bin/env -i <release>/python/bin/python3 -I -S -B <release>/launcher.py mcp`, an empty
environment. Any failure prints one fixed line, `{"schema_version":1,"error_code":"runtime_not_installed"}`,
and exits 1.

The shim only chooses a candidate path. The launcher then re-admits the receipt, the index and every
file of that release tree by hash before it imports any of the package's own code, and it admits only the
release the receipt names, so a pointer to any other tree fails there. What runs before that check comes
from the same candidate tree and is verified only afterwards: the interpreter (`python/bin/python3`,
started with `-I -S -B`, so no site directory, no user path and no `PYTHON*` variables) and the
standard-library modules it loads at startup, `launcher.py` itself, and the standard-library modules the
launcher imports before admitting (`ctypes`, `hashlib`, `json`, `os`, `pathlib`, `platform`, `stat`,
`sys`, and what they import). Until then only the shim's pointer and path checks and the tree's owner-only
modes bind that candidate; a process that can write as the same user is outside these checks.

**What the plugin trusts.** Every plugin file, `bin/install`, `bin/mcp-entry`, `.mcp.json` and
`release-pins.json` alike, comes from one commit: whatever commit of this repository's branch the user's
marketplace clone last added or updated to. The marketplace entry names the plugin by a relative path
with no ref or commit, nothing pins a plugin commit, and a marketplace update can change the scripts and
the pins together without a separate review. The trust root of the plugin path is therefore that branch.
Claude Code runs `bin/mcp-entry` as soon as the plugin is enabled, before any check of `release-pins.json`
the user makes, and `bin/install` runs when the user starts it; both run as the user, and no signature,
Team ID or pin covers either script. The Team ID check binds the downloaded seed, not the scripts that
download and run it: a branch with tampered scripts and matching pins passes every pin check. The only
check is to read the files (both scripts are short POSIX `sh`) before installing or enabling the plugin,
and to compare every plugin file with `plugins/telegram-local/` at the release tag
([Claude Code plugin](install.md#claude-code-plugin), step 3). That comparison shows the clone is at the
tag; it does not protect against whoever can push to the repository, who can move the tag too.

**The runtime installer.** `bin/install` takes no argument. Its trust input is the plugin's own files,
`bin/install` and `release-pins.json`, as the user's marketplace clone holds them (above). In a release,
`release-pins.json` is the release build's own file, copied byte for byte in the release commit, which
carries the tag ([releasing](releasing.md), section 4), so the plugin copy at the tag `v<version>` is the
reference to compare with. Before running the installer, inspect `<plugin root>/release-pins.json`: `team_id` must be
the publisher Team ID `256X5MUA6S` stated in the README and the install guide, and `version` the release
you expect; compare the file with `plugins/telegram-local/release-pins.json` at that tag. No environment
value, argument or downloaded byte selects a URL, pin or path. The pins file must be exactly the
canonical bytes (sorted keys, two-space indent, one newline) of the one schema
(`packaging/release_pins.py`) and its `version` must equal `plugin.json`'s. Unreleased pins refuse
(`release_not_published`), and published pins must carry a Team ID. Each of the four
assets comes from the fixed GitHub Releases URL pattern through `/usr/bin/curl -q --proto =https
--proto-redir =https --tlsv1.2 --fail --silent --location --max-redirs 5`, with a 30-second connect
timeout, a per-asset time limit and `--max-filesize` set to the pinned size. There is one attempt per
asset, no retry and no resume, so assets of different uploads are never mixed. They land in a `0700`
folder created under your home, and each must match its pinned size and sha256 before anything runs.
Then the pinned `bootstrap.sh` installs with `--claude-code plugin --seed-team <pinned Team ID>`.
`curl` sets no quarantine, so Gatekeeper never evaluates that seed; the Team ID check of its Developer
ID designated requirement, on top of the sha256 pins, binds it to the Team ID the pins name. Both come
from the plugin's files, so they bind the seed to the publisher only as far as those files are the
reviewed ones and their `team_id` is `256X5MUA6S`. The plugin never passes `--adopt-legacy`.

In plugin mode the installer writes no Claude Code user-scope entry and no `~/.claude/skills` copy. An
existing user-scope entry is only noted (`claude_code_user_entry: present`), never removed. Every
plugin tool name, `mcp__plugin_<plugin>_<server>__<tool>`, fits the client's 64-character limit
(`product_identity.check_names`).

## Signing and Team ID trust chain (0.53 candidate)

This is the chain from a downloaded kit to the running interpreter, outermost first. The owner
provisioned the Developer ID Application identity and the notary profile on 21.10.2026 (the signing
preflight returned `notary: ok` for Team ID `256X5MUA6S`). The Developer ID steps have still not run:
the signed and notarized build, its Gatekeeper observation and publication are owner-session rows. The
ad hoc dry run exercises the same code with the identity `-`.

**What binds what.** The trust root of the curl path, and the reference the plugin path is compared
with, is `release-pins.json` as committed in the repository at the release tag
(`plugins/telegram-local/release-pins.json`), never the copy published next to the assets: whoever can
replace the assets can replace that copy too. The plugin's `bin/install` reads its own copy, the one in
the user's marketplace clone, which holds the same bytes only while the clone is at that tag (see the
runtime installer above). The release build writes
the file in one schema (`packaging/release_pins.py`) and the release commit copies it byte for byte
([releasing](releasing.md), section 4). The pins name the Team ID and bind `bootstrap.sh`, the seed, the
index and the archive by sha256 and size; a user checks all four and compares `team_id` with the
publisher Team ID `256X5MUA6S` stated in the README and the install guide before running anything
([curl path](install.md#curl-path)). The tag binds the pins only as far as the repository is trusted:
anyone who can push to it can move a tag or change the file, which is why the Team ID is compared with
a value published outside the release. On the DMG path the DMG's Developer ID signature and stapled
ticket cover the kit, including `Install.command`, `bootstrap.sh` and the pins embedded in
`Install.command`; spctl accepts any notarized team, so the DMG's `TeamIdentifier` is compared too.

1. **Pins.** The seed's sha256 and byte count and the index's sha256 are literals in `Install.command`,
   rendered from the kit's `release-pins.json`; on the curl path they come from the tagged repository
   copy above and on the plugin path from the plugin's copy, and both also pin `bootstrap.sh` itself.
   `bootstrap.sh` checks the seed against
   them before running it. The seed then authenticates the archive and every member against the index
   before extraction, and the launcher re-hashes the installed tree at every start.
2. **Team ID, `--seed-team`.** With `--seed-team <TEAMID>`, after the pins and before the seed runs,
   `bootstrap.sh` runs `codesign --verify --strict -R` with the requirement
   `native_runtime.designated_requirement(TEAMID)`: Apple's anchor, a certificate chain through Apple's
   Developer ID CA, a Developer ID Application leaf, and `subject.OU` equal to the Team ID. An unsigned,
   ad hoc or other-team seed refuses as `seed_signature_invalid`. The pins stay; the Team ID narrows them.
   A Team ID alone was rejected, because it would trust anything the team ever signs. `Install.command`
   is rendered with the Team ID and a published kit without one is refused at render time
   (`team_required`).
3. **Manifest v2.** `publisher {kind: adhoc|developer-id, team_id}`, `publication
   unreleased|published` and `source_completeness incomplete|complete`. `published` requires `complete`
   and a Developer ID publisher. A v1 manifest stays admitted, as unreleased and incomplete only, and
   pairs only with an ad hoc lock.
4. **Native lock v2.** Every pin must equal one reviewed row of the approved-pin table in
   `native_runtime` (source commit, TDLib version, schema hash, minimum OS, ABI, license source, OpenSSL
   version, build method) and the unsigned artifacts must equal the v1 pins. An `adhoc` lock has no
   Team ID and ships the unsigned bytes. A `developer-id` lock ships different bytes and its Team ID
   must be listed in the row. The one row lists the publisher's team, `256X5MUA6S`; any other Team ID
   needs a reviewed commit to the table before its lock is admitted. At load each native passes
   `codesign --verify --strict`, plus the
   Team ID requirement for `developer-id`, and the manifest's publisher must equal the lock's kind and
   Team ID (`publisher_mismatch`).
5. **Signing.** `packaging/sign_payload.py` signs every Mach-O found by its magic, matched against the
   payload inventory, inside out: dylibs, bundles, executables, `python3` last, each with
   `--options runtime` and, for a real identity, a secure timestamp. The identifiers are stable: the
   product prefix plus `python3` for the interpreter, the relative path for the rest.
6. **Entitlements.** Only `python3` carries any. The build re-signs a scratch interpreter with each
   candidate set, smallest first, and runs probes (every distribution imports, TDLib answers its version,
   a Security.framework Keychain query returns not-found, `osascript` runs); it accepts the committed
   plist only if that is the smallest passing set
   (`entitlements_unproven` otherwise). Result for the ad hoc identity: no entitlements,
   `allow-unsigned-executable-memory` alone, or `allow-jit` alone fail with different Team IDs, and
   `com.apple.security.cs.disable-library-validation` is the minimal passing set, so the ad hoc plist
   carries it. The Developer ID plist is empty and unproven until the owner's signed run; the
   broker-under-launchd and connect-helper probes also wait for that run. `allow-dyld-environment-variables`,
   `get-task-allow` and `disable-executable-page-protection` are refused everywhere
   (`entitlement_forbidden`).
7. **Kit check before notarization.** Every Mach-O the kit ships (the seed and each payload file that
   `release.tar.gz` holds, bound to its index hash) must carry the Developer ID requirement with the
   Team ID, a secure timestamp and the hardened runtime, or the build stops (`signature_refused`). An ad
   hoc seed in a real build stops it as `seed_unsigned`. In the dry run the same check requires ad hoc
   signatures.

**Notarization scope.** `notarize_release.sh` submits two things. The DMG is signed with the identity,
submitted, required to be accepted with zero issues, stapled and validated; its signature is checked
against the Team ID requirement before submission and again after stapling, and `spctl` must accept it as
notarized Developer ID. Separately, a zip of the signed payload tree is submitted so the notary service
registers every Mach-O. The shipped `release.tar.gz` is not submitted: the service does not look inside
it, so its members are not notary-inspected, and its integrity rests on the pinned index and the seed
(step 1), not on Gatekeeper. `bootstrap.sh` keeps the quarantine attribute `cp` copies to the staged
seed, so that Gatekeeper can evaluate it and, because a bare Mach-O cannot be stapled, fetch its ticket
online. This is the design and has not been observed: no signed seed has run, and on the DMG path it
also depends on the mounted volume's files carrying quarantine at all (an owner-session observation).
The seed creates the extracted files with plain `openat` calls and copies no extended attribute, so they
carry no quarantine and Gatekeeper does not evaluate them individually; this has not been confirmed by
listing attributes on a real install. A file fetched with `curl` has no quarantine, so on that path only
the pins from the tagged repository copy and the Team ID check stand between the download and the seed.
`Install.command` itself is not code-signed, so no notarization ticket names it; the DMG's signature and
ticket cover the image it ships in. How Gatekeeper treats the double-clicked script from a stapled,
quarantined image is a known limit until the owner-session run observes it.

**Keychain.** The managed database key's Keychain access is tied to the code identity of the runtime's
interpreter. A different signing identity than the one that created the item can make macOS ask once;
the owner's migration run is the test. The stable interpreter identifier is meant to keep the
designated requirement the same across releases.

The chain proves who signed what was shipped and that the installed bytes equal the index. It does not
protect against a hostile process running as the same OS user, and a certificate or pin the owner
accepts is a trust decision, not a legal sign-off.

## Upgrade, rollback and uninstall invariants (0.53 candidate)

All three are journaled transactions under the installer lease (`.install.lock` in the installation root).
Each effect is recorded write-ahead and happens at most once; a rerun resumes from the journal, and a
different action or kit started over an unfinished one refuses (`transaction_pending`).

- **Session, enrollment and Keychain are not written.** The installer touches the installation root, the
  Codex subtree, the owned skill copies, the Claude Code and Claude Desktop entries it owns, the broker
  plist and, on an upgrade, rollback or adoption of a service, one line of `runtime.toml`:
  `expected_package_version`. Every other byte of that file, capabilities included, stays, and a file
  that cannot be changed that way refuses (`policy_conflict`). `enrollment.json`, the managed key, the
  TDLib session and the Keychain are never written; their hashes are only read.
- **Pending-send gate.** Before the transaction exists, inside it once the release is copied, and again
  right before the broker is booted out, a running exact receipt-bound (or adopted) broker is asked,
  through its own interpreter and only with what every adopted release ships (0.51 and 0.52 included),
  whether any client context is connected. A context may hold a draft or an unresolved send outcome.
  Anything but "none" (including an unknown answer) refuses (`pending_send_unresolved`). The bootout
  follows the last check directly and precedes every client write, so a refusal leaves every client
  entry, the plist and the session unchanged; after the first check, the copied release tree and the
  unfinished transaction stay until the same command is repeated. Only the exact label is
  booted out, an owned broker only after its live registration matches the on-disk plist, and it is
  bootstrapped again after the commit only if it was running. An uncertain send is never retried.
- **Downgrade refused.** A package version that sorts below the installed one refuses
  (`downgrade_refused`); an unparsable version refuses the same way. Going back is only `rollback`.
- **Rollback only to unchanged state.** It refuses (`rollback_refused`) unless `enrollment.json` is
  byte-identical to its state when the upgrade began, `runtime.toml` differs from its earlier bytes only
  in the version line, and the previous release's manifest and Codex ownership still match.
- **Commit order.** The new release tree is copied next to the old one, the broker is stopped and its
  plist rewritten, the clients follow, then `receipt.json`, and `current.json` last. A release is admitted only when the receipt and pointer
  name it, so an interrupted change leaves the previous release launchable.
- **A late refusal restores the service first.** Any refusal after the broker stop and before
  `current.json` switches (the same for a rollback, and for an adopting first install before
  `current.json` exists) first puts the `runtime.toml` version line back if the transaction rewrote it
  (to the value journaled before its first rewrite; a line changed since stays), writes the previous plist
  back byte for byte, from a copy kept in the transaction (`service.previous`, its hash journaled) before
  the first rewrite, and bootstraps the previous broker again only if it was loaded. Every run before the
  commit checks the version line again, so the repeat pins the new version once more. The restore is
  journaled: a write-ahead record carrying the refusal's fixed code precedes its effects and a closing
  record follows them, every effect is decided from the files and launchd, and a run that finds the
  write-ahead record without the closing one finishes the restore and refuses with the recorded code. It
  is idempotent by state like the stop: the target's broker, which a login after a kill past the plist
  rewrite starts from that plist, is booted out first, without the pending-send gate (short of the commit
  its release is not admitted, so it serves no session); the previous broker loaded again stays; a broker
  under the label that is neither stays running and refuses (`service_conflict`). It also removes the
  temporaries killed client writes left beside the Codex config, the Claude Desktop config, the plist and
  `runtime.toml`, each only by the name journaled before its creation (and, once journaled, its inode);
  uninstall and the commit do the same for a change parked with one. A plist changed since the stop is
  never overwritten (`service_conflict`). What stays: the copied release, the unfinished transaction and every
  client entry written before the refusal, which already names the new release and so cannot start the
  server until the repeat commits (only a refusal at the switch of `current.json` itself comes after
  `receipt.json` names the new release, and then neither release starts until the repeat or an uninstall).
  The same command stops the broker again (gated again) and resumes. Cycles are not limited, since each
  is a run the user starts; the cycle lives in one journal record replaced in place, and a finished
  restore drops its cycle's write records, so the journal does not grow with their number.
- **The service step is idempotent by state.** Every run before the commit, whatever stage the journal
  records, makes the service the target's: a previous definition that was loaded again after the stop is
  gated and booted out again (also when no broker ran as the change began; then none is started after the
  commit), and the plist is rewritten until it is the target's. After the commit only the target's plist
  is bootstrapped, and only if a broker ran as the change began; any other definition refuses
  (`service_conflict`) and nothing is started. A kill anywhere in a repeat's stop, or a login after it,
  therefore cannot leave the old broker running on the new pin.
- **A parked change has two ways out.** An upgrade or rollback refused before its stop, or after a late
  refusal's finished restore, is parked: its repeat resumes it once the named object is put back or
  removed, and `uninstall` goes over it, removing what the receipt the change started from owns plus what
  the change's journal binds (its Codex entry once written, its skill copies, the Claude Code and Claude
  Desktop entries it recorded `registered`), each only while it is exactly that object; its adoption
  backups are kept. `rollback` and another kit refuse (`transaction_pending`). A change interrupted with
  its broker stopped or its restore unfinished is not parked: uninstall names it too. A parked adopting
  first install has no installed release yet, so only its repeat ends it.
- **Client edits made meanwhile are merged.** Each client file is written over its current bytes. Only a
  change of our own Codex entry or skill copy (or, when adopting, of the adopted foreign Codex entry)
  after the transaction recorded it refuses, with the same fixed code before or right after our write
  (`target_conflict`, or `foreign_registration` on a first install); a changed Claude Code or Claude
  Desktop entry is left alone and reported `failed`, and an entry or skill copy removed meanwhile, also
  one this transaction already wrote, is written again before the commit. A skill copy this transaction
  published, edited and then put back in place is its own again only while it is the very object an attempt
  published (the binding its journal records) and holds exactly the target's bytes; an edit that keeps the
  binding but not the bytes still refuses (`skill_conflict`). Claude Code entries are
  written only by its CLI, which keeps its neighbours.
- **Owned objects only.** Replacement and removal require the exact bytes or filesystem object the
  receipt or journal binds. The installation root itself is such an object: `uninstall` removes it whole,
  by the inode the install intent binds, with whatever else was put inside it; install, upgrade and
  rollback refuse a foreign top-level name in it (`runtime_conflict`), and the guide tells users to keep
  nothing of their own there. An entry or skill copy that changed is kept (`kept`) or refuses. A client
  entry is owned only when an install, upgrade or rollback of this installation wrote it, and it stays
  owned through later steps that ended otherwise, wherever it is still exactly that entry. A Claude Code
  entry is changed only through the located `claude` CLI, each attempt recorded before it is made, and
  an entry that is still exactly ours after its removal attempts refuses as `uninstall_incomplete`.
- **Adoption is explicit.** `--adopt-legacy` must be the last token of the `bootstrap.sh` command and is
  never passed by `Install.command` or the plugin. It applies to a first install and an upgrade. Each
  foreign entry or plist (Codex, Claude Code user scope, Claude Desktop, the broker plist) is replaced
  only after its exact bytes (at most 1 MiB each, at most 64 records) are recorded in the transaction,
  `adopted.json` and the receipt. A foreign broker passes the pending-send gate first. `uninstall` moves
  every backup beside the removed root and reports it as `legacy_backups`. Adoption checks nothing about
  what it replaces beyond its size and, for the plist, that its bytes stay the backed-up ones until they
  are replaced: whatever `telegram_search` entry and broker plist it finds is backed up and replaced, not
  only a 0.51 or 0.52 one. The pending-send gate runs the interpreter the foreign plist names, so adopting
  trusts that interpreter. The `--adopt-legacy` recipe adds no check of its own: it runs `bootstrap.sh`
  from the kit folder as given, so the release is checked only as far as the user first checks the four
  kit files and the Team ID against the pins from the trust root, as on the curl path; `bootstrap.sh` then
  checks the seed (sha256, size, Developer ID requirement for that Team ID), and the seed the index and
  every archive member. Backups are kept, never restored automatically.
- **Leftovers only when provably the installer's.** Beside the root, under the parent directory's lock, a
  killed first install's staged root is removed only when every entry in it is one the installer creates
  (an empty one only under one of its fixed attempt names), an older intent temporary only when it parses
  as the intent, and the root an uninstall retired only while it is the exact directory the install
  intent binds; the intent goes last. A lookalike stays, also one under the intent's recorded staged name
  after the root's publication. An unfinished first install (its exact bound staged root) or an uninstall
  short of its retiring rename is named by the next action (`transaction_pending` with `pending_action`),
  never half-removed; uninstall reads the intent and makes that check before it removes anything. After the
  retiring rename, the uninstall's last journaled step, the next install or uninstall finishes removing
  the retired root, also when an interruption left it partly removed.

The release trees of earlier versions stay in `releases/` until an uninstall removes the root. The
checks are cooperative: they detect drift and refuse; they do not make arbitrary editors take the lease.

## Retained-selection boundary (B2B candidate)

The default-off `retained_selections` capability adds one closed issuer and exact
source alternatives on existing operations. Request models, the proxy's strict
request check and the broker enforce exactly one raw source or selection, plus an
independent exactly-one reply source where applicable. Since the 0.54 candidate the
finalized SDK input schemas state the rule in the tool description instead of a
top-level `allOf`/`oneOf`; enforcement is unchanged. Target capture additionally requires
`verified_targets`; original operation capabilities remain required. Contract 1 and
framing 2 remain, while the schema fingerprint honestly changes for 44 tools (at that slice).

A memory-only selection binds B2A owner/client/session incarnation, observed numeric
account and revocation epoch, schema fingerprint, ordered anchors or immutable
artifact hash/size/metadata, original provenance, target dependency, and fixed wall
and monotonic expiry. No persisted raw body, entity, filename, local path or token
registry is created. Capacity is 16 live entries/four active operations including
pending captures per owner, without eviction or sliding refresh. Since the 0.54
candidate the fixed lifetime comes from trusted `selection_ttl_seconds` (strict
integer 60..1800, default 900, previously a fixed 300 seconds), still capped by the
artifact or verified-target expiry. The broker's registry takes it from its loaded
policy; the proxy refuses any issued expiry beyond its own loaded value. The setting
is part of the configuration fingerprint, so mismatched components report
`config_mismatch`. A caller cannot change it.

Exact projection hashes the complete bounded raw text/caption, original whole-scalar
UTF-16 entity coordinates and inert type payloads, identity/date/edit/reply/topic/album
and the existing selected attachment variant. Stable remote unique identity is
hashed separately from in-generation file ID and exact raw name/MIME/size and
applicable dimensions/duration. Unknown inert entity payloads detect changes only;
they add no reply rendering, replay, outgoing entity, navigation or send authority.
Serialization above 64 KiB/message or aggregate raw text above 100000 characters
refuses; no prefix hash or sanitization-equivalent shortcut is used. Titles, sender
display names, transfer progress, local paths and view/unread counts are excluded.
The best-effort `getMessageLink` result is excluded too: it is not in the private
projection, and the proxy's recapture and selected-read comparisons drop only
`source.telegram_url`, so a link that appears or vanishes neither refuses nor renews
a selection while every projected fact still must match.

Ownership admission precedes metadata, byte hashing, provider work and extraction.
Local provenance makes zero provider calls; Telegram/derived provenance is frozen
from the original download and rechecked after transfer/store and every selected
use. Actual raw message returns during selected message operations also match the
captured projection, catching transient drift that later reverts. Any failed source
discards all earlier rows and outputs. Observed account drift revokes B2A provider
grants and selection epochs permanently; switching back cannot resurrect them.

Provider observations remain outside authority, selection, target, state and draft
registry locks. Pure revocation fences cover constructed content, derived IDs,
paths, cursor publication and draft insertion. Unpublished candidate drafts are
tracked per selected operation and atomically discarded on interrupted publication
or preview failure. Continuations bind original selection/selector/hash/provenance
and cap real cursor state to selection expiry; both grants are checked on each call.
A refusal contains only fixed status/detail and discloses no owner, existence,
anchor, name, path, hash, preview or other source content.

Selections do not shorten artifact/upload TTLs, delete cached bytes, select a
recipient, relax same-chat/topic/source admission, or satisfy send approval. Raw
artifact paths retain their independent B2A authority. CLI upload and MCP client
owners are distinct; foreign receipt adoption is refused. Unobserved session
replacement is outside the claimed observation coverage. Synthetic guarded tests
are technical evidence; installed/native/live consumer gates and publication
remain separate.

## External NEW-byte delegation (B2C candidate)

Three closed owner tools add manifest-bound, one-use authority under existing
artifacts policy; no new send capability/default is granted. A private authority
has at least 256 bits of entropy. Only its digest, exact four-field manifest,
opaque independent result reference and lifecycle facts are retained in bounded
memory. No bodies, source paths or plaintext authority are persisted by the ticket
registry. The trusted helper receives a bounded closed authority+manifest envelope
only via stdin/IPC, rehashes before redemption, uses its own still-live lifecycle,
and never falls back or retries lost mutations. Helper operations are absent from
MCP registration. Secret transport is never an ordinary receipt or logged argument.

The exact original owner object/lifecycle/generation and already observed account
epoch are checked at each transition. Released/reused identity strings, broker
restart, fixed dual 900-second expiry or observed pre-consume epoch drift cannot
resurrect authority; switch-back does not undo drift. Offline issue/supply/consume
makes zero provider/account/readiness/auth calls. Unknown external changes without
an observation are outside detection coverage. The eventual local grant explicitly
has account_id=None and account_epoch=None; observed drift after consumption does
not revoke local reads. Receipts replay the existing fixed grant without renewal.

Lock order is shared upload registry, artifact authority, then broker metadata
state. Chunk decode/hash and finish fsync/copy/hash run outside revocation locks.
Finishing paths and shared reservations remain tracked until copying exits, so
legacy cleanup cannot retire them as orphans. Issued through complete tickets share
the legacy eight-slot budget; a redeemed stream consumes no second slot. Terminal
delivery receipts have a separate 32-entry bound and original ticket deadlines.
No grant or public artifact identifier exists before original-owner consume.
The final pure fence rechecks owner/supplier/state/epoch/deadlines/manifest/policy,
creates only the original-owner grant, installs metadata/receipt and covers the
broker response publication callback. Cancellation/release that wins refuses
without metadata. Unpublished bytes retain normal cache/quarantine cleanup rules.

Foreign result/read/metadata/page/XLSX/PPTX/draft admission refuses before content
or provider work. Pending/unavailable responses contain only fixed status/detail.
Consumed cancellation is unavailable and leaves the existing artifact grant
intact. Lost supplier replies never permit upload replay; owner consume can recover
only completed work. Exact recipient, unsent preview and approval remain separate.
Legacy plain helper artifacts still belong to the helper and cannot be adopted.
This is cooperative MCP isolation: trusted same-UID filesystem privilege and
explicitly authorized CLI exports remain, without a hostile same-UID secrecy claim.
Synthetic writer tests are technical evidence; installed/native/live and release
acceptance remain separate gates.

## Owner approval dialog (0.54 candidate)

Every send and change needs two approvals: `approved=true` in the task, then a click in a local macOS
dialog that only the broker opens, after its own revalidation and before the single-use claim. The
client cannot open, answer, skip or pre-fill that dialog.

**One dialog.** An `NSAlert` through `osascript -l JavaScript` (JXA). The script is a fixed constant;
only the product name is substituted, escaped as a JavaScript string. The rendered document and the
timer's seconds reach the script as command-line arguments after `--`, never as script text, so
untrusted content cannot become code. `Cancel` is added first and is the default button, with Return
set explicitly (AppKit would otherwise give a "Cancel" button Escape and leave no default). `Send` (or
`Apply` for a change) has no key equivalent. A read-only, fixed-size scrolling view holds the document
and never wraps. The window joins the owner's current Space and full-screen apps, and has no appear
animation. Without that, the proof found it off-Space, occluded and transparent. A 120-second timer,
registered for the modal run-loop mode, ends the dialog with AppKit's abort response. The script prints
exactly one token: `APPROVED`, `DECLINED` or `TIMEOUT`.

**Fail-closed decision.** The broker accepts approval only as `True` or the closed `ApprovalDecision`
value `approved`. Only that value is truthy, so the legacy `if not decision` checks keep refusing.
Everything else maps to a status that never sends or changes anything:
- `declined`: `approved` was not `true` (no dialog), or the owner clicked Cancel. Each has its own fixed
  detail.
- `timeout`: the dialog timer ended it.
- `dialog_unavailable`: no dialog decision exists. That covers a missing or failing `osascript`, a
  non-zero exit, any other output (including a token in a different case or with extra text), the outer
  125-second process limit, an exception inside the script, any exception from `subprocess.run` itself
  (including a `ValueError` for a NUL byte or an unencodable argument), a timer value outside 1–120 seconds, or
  arguments that do not describe exactly one preview bound to the broker's own draft. Any other value an
  injected prompt returns, such as a string or a truthy object, is also `dialog_unavailable`.

The dialog runs through `subprocess.run` with a bounded timeout. Details are fixed strings; no input,
dialog text or provider message is echoed.

**One renderer.** Every kind uses the same document: a plain-language summary (action, chat, account,
expiry), the exact payload, the reading rule, and a technical block with the draft ID and hashes. The
kinds are plain text, file, topic or formatted text, text reply, reply with a file, forward or copy,
native video, archive move and own text edit. Untrusted strings are rendered under
`forward_drafts.READABLE_RULE`:
- A multi-line text or caption keeps its real line feeds. Each line is its own row behind the gutter
  `│ `, and `«` and `»` mark its exact ends.
- Short strings (titles, names, URLs, covered spans) are inline `« »` spans, where every line break is
  marked.
- Rows are split at 96 columns (wide characters count two) onto rows that start with the marker `┆`
  plus exactly one space. Any further space after the marker is text: a split before a space leaves it
  visible (`┆  word`), while a long word or URL split at the width continues directly (`┆ xxxx`).
- The reply target is the bound rendering of the message replied to, not Telegram's raw text (styled
  and media targets are JSON renderings even when nothing was cleaned), so its label always says
  `(rendering)`, and `(rendering, cleaned, not exact)` when cleaning changed it; its sanitized flag
  always sits directly under it.

No untrusted string can begin a row that looks like a dialog line. The topic, reply and artifact
dialogs no longer print raw Telegram text, as they did in 0.53. Each preview is revalidated by its
model and compared with the broker's own recipient, title, hashes, size, text, caption, file name and
kind; reply-artifact previews gained that comparison in 0.54.

**Proof and fallback.** The JXA dialog was run for real on the timeout path from a temporary user
LaunchAgent (background process type, Aqua session, isolated HOME) and directly. The record is in
`docs/plans/0.54-conversational-usability-spec.tasks.md`, section C. The spec's fallback (the earlier
`display dialog` with a short summary and a truncation notice) was not needed.

Residual risks: the dialog shares the screen with other software of the same user, which can draw over
it or send synthetic clicks, as with the earlier dialog. A single space at the end of a payload line is
invisible. The JXA bridge and AppKit behaviour are macOS components outside this project.

## Exact forward/copy boundary (F14 candidate)

`prepare_forward_send` and `send_prepared_forward` add the default-off `forward_send`
capability, which is effective only together with `send`. The conjunction is
enforced by the SDK guard, by the proxy before it connects, by the broker before
dispatch, and again for status and receipt replay of a forward draft; with either
capability missing the tools are not listed in tools/list and fail with `capability_disabled`. The
authority is exactly one existing message from one exact chat to one exact chat, as
a `forward` (recipients see the original sender) or a `copy` (a new message from
the account, no attribution). It never covers several messages, albums, topic
destinations, scheduling, paid or silent options, content protection, secret chats
or caption edits, and a retained selection cannot name a source. `forwardMessages`
is the only added TDLib request type; its closed shape is listed in the authority
boundary above, and the random `sending_id` is drawn per attempt and excluded from
the preview digest.

The broker, not the caller, reads the source: one serialized provider read resolves
the source chat (secret chats refused), reads the message and reads
`getMessageProperties`. The message becomes a closed version-3 projection of at most
64 KiB: chat, message ID, typed sender, author signature, via-bot ID, forward flag,
date, edit date, text or caption with entity types and raw UTF-16 offsets, admitted
link-preview options and link-card URL and, for media, sizes, measures, names,
presentation flags and a hash of each file's remote unique ID. File IDs, local paths
and volatile state (views, reactions, interaction info, the link card's title,
description, site name and media) never enter it.
A missing or mistyped safety field (`self_destruct_in`, `auto_delete_in`, ephemeral
and scheduling fields, `is_secret`, the property booleans) is never treated as
safe: the source is unavailable or unsupported. `source_sha256` is shown to the
owner, bound into `preview_sha256`, compared at send time before the dialog and
compared again inside the provider call after approval. Drafts are bound to the proxy client and current account; a foreign
client, new proxy identity, account change, broker restart, supersession or expiry
returns the content-free `expired`.

Because the provider forwards its own stored message, anything the owner cannot
review would be delivered unseen. Preparation therefore refuses with the
content-free `hidden_source_content` for a reply markup (buttons carry targets
the text does not show); for any text or caption entity outside the allowlist of
fieldless visible types (Bold, Italic, Underline, Strikethrough, Spoiler, Code,
Pre, BlockQuote, ExpandableBlockQuote, Mention, Hashtag, Cashtag, BotCommand, Url,
EmailAddress, PhoneNumber, BankCardNumber), so TextUrl, MentionName, CustomEmoji,
PreCode, MediaTimestamp, DateTime, unknown types and allowlisted types carrying a
field are refused; for a link-preview-options URL or a link-card URL that is not
exactly the covered text of a Url entity or a whole whitespace-delimited token of the
visible text (a prefix or a substring of a longer link refuses); and for a name (file
name, MIME type, audio title or performer, author signature) that sanitizing would
change. Entities must be exact
`textEntity` records on whole-scalar boundaries of the raw text, otherwise
`unsupported_source_content`. A same-second edit that changes an entity type or adds
a hidden target is caught at send time: the projection binds every entity type and
the re-read refuses fielded entities (`source_changed`). Refusal codes are fixed;
none echoes source content or provider text.

Attribution is part of the reviewed facts. `forward` exposes the original sender, so
the preview and dialog say so, and a source that already carries forward information
refuses `source_is_forward` in that mode because nested attribution cannot be
shown. `copy` sends none; a copied forward is marked in the preview and dialog,
author signature and via-bot ID are hashed and displayed when present, and
`remove_caption` is shown as `Caption: removed — not sent` without printing the
caption. Mode, `remove_caption` and the whole closed request are bound into
`preview_sha256`; a coherently re-signed preview for another mode, message or
recipient is rejected by the proxy's request-scope check.

Telegram-originated strings are untrusted data. The MCP preview carries text and
captions as a marked reversible JSON string in which only ASCII letters, digits, `_`
and `-` are literal, so homoglyphs, bidi and zero-width controls and whitespace are
visible as `\uXXXX`; one decoded layer is exactly the hashed raw text, and entity
offsets address it in UTF-16 code units. The local dialog is written for a human (see
[Owner approval dialog](#owner-approval-dialog-054-candidate)): letters of every script stay
literal; the text or caption is an exact payload behind the `│ ` gutter, one row per real line;
and each entity's covered span, each link-preview URL, the chat titles, the author signature and
the media names are enclosed in `« … »`. One reading rule, printed in full, states what is
rendered as reversible `⟦U+XXXX⟧`: control characters (including tab and every line break other
than a payload's own line feeds), format, private-use and unassigned characters; every Unicode 16
default-ignorable code point except one U+FE0E/U+FE0F directly after a non-selector character
(emoji presentation); line, paragraph and non-ASCII spaces; each U+0020 after the first in a run;
U+2800; characters that NFKC changes; combining marks after the second in a row; the brackets
`« » ⟦ 〚 〛`; and, inside a word that also has ASCII letters, Cyrillic, Greek, Armenian, Cherokee
and Lisu letters and non-ASCII Latin letters without a canonical decomposition. Because literal
`«` and `»` are marked and every payload row starts with the gutter, untrusted content can neither
close its span nor start a dialog line. The dialog lists every entity, the link-preview options,
the link-card URL and the media facts, and prints account, expiry and, in its technical block, the
draft ID and hashes. Names are admitted only when sanitizing leaves them unchanged, so they are
shown exactly; one longer than 255 characters refuses. The dialog is rebuilt from the stored
immutable draft and validated against recipient, hash and mode before display; a mismatch shows
no dialog and is `dialog_unavailable`.

Protected content cannot be forwarded or copied. `protected_content` applies to
both modes when the source chat is protected or the message properties report
protection by the current or another user; independently, `can_be_forwarded`
(forward) or `can_be_copied` (copy) must be strictly true, else
`source_not_forwardable`. All five facts must be present as strict booleans. They
are checked at preparation, again at send before the dialog (reported as
`protected_content` or `source_not_forwardable`, with any other eligibility
refusal reported as `source_changed`), and once more inside the provider call after
approval (reported as `local_failed`, with no transport).

The claim is single-use and taken only after owner approval. One `forwardMessages` is
sent and never repeated: not after a timeout, a malformed or lost result, a
transport error, an authorization change or a restart. Before registration and
transport a refusal is `local_failed` and the observation is discarded; afterwards
the outcome can only be `sent`, `failed` or `outcome_unknown`, and a repeated send
replays the receipt without a dialog. Observations hold only IDs, state and times
(no text, paths or provider error strings; 900 seconds, 4096 entries). The
`forwardMessages` result must be one pending outgoing message in the recipient chat
with the source's content type and the attempt's `sending_id`, and the final update
must carry the same temporary ID. A malformed, mismatched or multi-message result is
a terminal conflict that never reconciles to `sent`, even if the success update
arrived first; a success that overtakes the result is held until the result
validates. Only a numeric provider error code appears in a fixed detail.

Residual risks, recorded rather than closed:

- Thumbnails, album and video covers, storyboards and alternative videos are copied
  by the provider but neither projected nor shown, so the dialog cannot show them.
- `import_info` sources are not refused. A link card's title, description, site name
  and media are delivered with the message but neither projected nor shown; only its
  URL is checked, bound and shown. A provider that normalizes the card URL (for
  example adds a scheme to a bare domain) makes a legitimate message refuse.
- The dialog marks look-alike letters only for the listed scripts and undecomposed
  Latin letters inside a word with ASCII letters, plus NFKC-unstable forms; look-alike
  words written entirely in one script appear literally, and legitimate letters such
  as ß or ł are marked when mixed with ASCII. A single U+FE0E/U+FE0F after any
  non-selector character stays literal, and a single space at the end of a payload line
  is invisible. The escaped MCP preview and the hash cover all of these. Since 0.54 the
  dialog view never wraps: the renderer splits rows at 96 columns with a `┆ ` continuation
  mark, so an untrusted word no longer starts an unmarked visual line.
- The source and recipient reads are cached or offline provider evidence, not an
  atomic Telegram guarantee: a remote edit can race the final check, and the delivered
  message is whatever the provider holds at transport time.
- Confirmation does not verify that the provider applied the requested attribution;
  Telegram may convert a forward to a copy for some chats. The receipt records what
  was requested.
- For the account's own Saved Messages the provider's `is_outgoing=false` on the
  returned message is accepted when sending ID, chat and temporary ID match;
  elsewhere an incoming flag never matches. This rests on an assumption about
  provider behavior that has not been checked against a live account.
- Real owner-approved forward and copy delivery with readback has not been run; the
  evidence is a synthetic provider. The same-UID cooperative client boundary is
  unchanged, with no hostile same-UID secrecy claim.

## Native video boundary (F15a candidate)

`prepare_video_send` and `send_prepared_video` add the default-off `video_send`
capability, which is effective only together with `send`. The conjunction is enforced
by the SDK guard, by the proxy before it connects, by the broker before dispatch, by
preparation and send ownership checks, again for status and receipt replay of a video
draft, inside the provider call, and a last time inside the raw-dispatch fence around
the TDLib send; with either capability missing the tools are not listed in tools/list and fail with
`capability_disabled`. The authority is exactly one owned artifact, as one MP4 or MOV of
at most 64 MiB, to one exact chat, as a native video. It never covers albums or several
videos, replies, topic destinations, thumbnails or covers, spoilers, self-destruct
timers, captions with styles, transcoding, videos over 64 MiB, secret chats or caption
edits, and a retained selection cannot name the artifact. `inputMessageVideo` inside
`sendMessage` is the only added request constructor; its closed shape is listed in the
authority boundary above, and the random `sending_id` is drawn per attempt and excluded
from the preview digest. At the broker, a `selection_id` or `reply_selection_id` in the
payload of any operation without selection support, including both video tools, is
refused as `invalid_request` under every policy before any selection, artifact or
provider work (with `retained_selections` enabled it previously escaped as a
`KeyError`).

Eligibility is decided locally by a bounded, stdlib-only ISO-BMFF reader; there is no
subprocess, no `ffprobe` and no decoder. It opens the cached artifact path without
following symlinks, requires a regular file whose size equals the recorded size, reads
box headers by seeking and the `moov` payload into memory, and refuses a file that
changes in size or modification time while it is read. Its bounds are 64 MiB for the
file, 4096 boxes, nesting depth 8, 16 MiB for `moov` and 4096 bytes for the `ftyp`
payload; truncated, overlapping, oversized or non-progressing boxes refuse. It never
reads `mdat`. Every refusal is the one fixed code `unsupported_video`, with no detail,
no chained exception and nothing from the file or its path; an artifact over 64 MiB is
refused as `video_too_large` before it is opened. Accepted shapes are exactly one
non-fragmented `moov`, one H.264 or HEVC video track and at most one `mp4a` or
Opus audio track, a duration of 1 second to 3 hours, sides of 16 to 8192 pixels with an
aspect ratio of at most 20:1, and a plain 0, 90, 180 or 270 degree rotation matrix; see
the [probe rules](reference.md#size-limit-probe-rules-and-refusal-codes).

The resulting duration, dimensions, codec, audio and streaming layout are declarations
of the file's own boxes, not decoded or verified facts. The `VideoDraftPreview`
description, the dialog (under "Declared by the file (not decoded):") and the tool
description say so, and the approval covers those declared values, the exact bytes (by
SHA-256) and the recipient rather than the content of the media. The draft binds the
probed facts, SHA-256 and size to one immutable record. The preview digest is a
domain-separated version-1 SHA-256 over the draft ID, account, expiry, recipient,
artifact ID, name, size, SHA-256, facts, caption and the exact closed `sendMessage`
request (without `sending_id` and the staged path). The proxy recomputes the digest and
checks artifact ID, recipient and normalized caption against the request; otherwise the
result is `unavailable`. The preview is built and validated from the exact candidate
before the draft is inserted, so a refused preview leaves no orphan draft and supersedes
nothing.

Drafts are bound to the proxy client and current account, live in the same in-memory
registry as other drafts (internal kind `video`) and are not reachable through the
generic draft tools: `list_drafts` omits them, `get_draft`, `update_draft`,
`refresh_draft`, `cancel_draft` and `get_reply_artifact_draft` return `unavailable`, and
`send_prepared_text`, `send_prepared_artifact` and `send_prepared_forward` return
`error`. A foreign client, a new proxy identity, an account change, broker restart,
supersession or expiry returns the content-free `expired`. Preparing again for the same
owner, artifact and recipient supersedes the earlier pending draft. A foreign client
cannot prepare another client's artifact (`unavailable`).

At send time nothing is trusted from preparation. Before the dialog the broker repeats
the owner and policy checks, re-hashes the artifact against the draft's SHA-256 and size
(changed or missing bytes are `expired`) and resolves the recipient again, comparing
chat type, native ID and sanitized title (`recipient_changed`; a recipient that became a
secret chat or cannot be read is `expired`). After the dialog it checks owner, policy,
provider session and account again and only then takes the single-use claim. Staging
copies the approved bytes into a private `draft_`-prefixed directory under the staging
root and re-hashes them; only that staged copy is ever given to TDLib, and the confirmed
copy is retired. Inside the serialized provider call the account, policy, claim,
recipient identity and title are checked again, and the guard runs once more after the
observation is registered and immediately before transport. Any failure after the claim
and before transport is `local_failed` and the claim is consumed.

The claim is taken only after owner approval. One `sendMessage` is sent and never
repeated: not after a timeout, a malformed or lost result, a transport error, an
authorization change or a restart. Afterwards the outcome can only be `sent`, `failed`
or `outcome_unknown`, and a repeated send replays the receipt without a dialog. The
confirmation wait for a video is up to 120 seconds because uploads
take longer than text; the whole call, including the dialog and staging, stays within 330
seconds. If the wait ends first, the outcome is `outcome_unknown` and `get_send_status`
reconciles it later without a retry. Observations hold only IDs, state and times (no
text, paths or provider error strings; 900 seconds, 4096 entries); registration of a
video is refused if it also claims a reply, topic, forward, text, caption, voice or
entity expectation, or has reviewed facts outside the same bounds. The observation correlates by `sending_id`,
recipient chat and the returned temporary message ID, and the final message must be a
`messageVideo` carrying a `video` object. The reviewed duration, dimensions and size are
bound at registration but are not compared with the final message, because Telegram may
re-encode; a delivered video is therefore not turned into `outcome_unknown` by
recomputed facts. Only a numeric provider error code appears in a fixed detail.

The file name and caption are untrusted. The MCP preview carries each as a marked
reversible JSON string in which only ASCII letters, digits, `_` and `-` are literal. The
local dialog shows the name in `« … »` and the caption as a gutter payload, under the same
reading rule as the forward dialog (letters of every script literal; control, format,
default-ignorable, repeated-space and look-alike characters, `« » ⟦ 〚 〛` and line breaks other
than the caption's own line feeds shown as reversible `⟦U+XXXX⟧`), so untrusted content can
neither close its span nor start a dialog line. The dialog is rebuilt from the stored immutable
draft and validated against recipient, hash, size, name and caption before display; a mismatch
shows no dialog and is `dialog_unavailable`. File
names are normalized with NFKC before directory parts are removed, so a compatibility
solidus (U+FF0F, U+FF3C, U+FE68) cannot become a separator after the split. This applies
to every artifact-backed draft name: a document named `a／b.txt` is now `b.txt` instead of
failing at staging.

Residual risks, recorded rather than closed:

- The probe reads the cached artifact path after the store lookup. The draft binds
  SHA-256 and size, and claim and staging re-hash the bytes, so only a same-user rewrite
  of the owner-only cache between probe and hash could pair probed facts with other
  bytes. The facts remain declarations either way.
- A video send holds the serialized provider lock for its confirmation wait of up to 120
  seconds; other provider operations queue behind a slow upload.
- As for plain artifact sends, the final message's caption is not compared with the draft
  caption; correlation decides.
- Telegram may re-encode the upload. Confirmation is by correlation and a final
  `messageVideo`, not by matching the declared facts, and status reports no re-encoded
  facts.
- Only the attempting call is protected from a misleading `expired` (see the artifact
  ownership section). A client that ignores its `outcome_unknown` and calls again after a
  release or account switch reads `expired` and could prepare a duplicate. Extending the
  registry consult to later calls would rewrite the reply-replay refusals that existing
  artifact-reply suites pin (an owner decision, not made here).
- If the publication check fails after a successful `prepare_video_send`, the inserted
  draft (which superseded any pending one for the same artifact and recipient) is never
  returned and expires unused, as for every artifact-backed prepare.
- Real owner-approved video delivery with readback has not been run; the evidence is a
  synthetic provider and synthetic ISO-BMFF fixtures. The same-UID cooperative client
  boundary is unchanged, with no hostile same-UID secrecy claim.

## Archive and own text edit boundary (F17a candidate)

`prepare_chat_archive`, `apply_prepared_archive`, `prepare_text_edit` and `apply_prepared_text_edit`
add two default-off capabilities, `archive_chats` and `edit_own_text`. Each is effective only together
with `send` (the existing "this tool changes Telegram state" authority) and `read` (preparation reveals
content; owner decision 20.10.2026), and neither enables the other.
The conjunction is enforced by the SDK guard, by the proxy before it connects, by the broker before
dispatch, by preparation and apply ownership checks (repeated after the owner dialog and before the
attempt is reported), again for status and receipt replay of a draft of that feature, inside the
provider call (the pre-send guard checks both capabilities and the current claim) and a last time
immediately before transport; with either capability missing the tools are not listed in tools/list and fail with
`capability_disabled`. The authority is exactly one chat moved between the Main and Archive lists, or
exactly one own outgoing plain-text message whose whole text is replaced. It never covers caption edits,
styled edits (the request carries no entities), other people's messages or non-text messages, pin and
unpin, deletion, chat folders, bulk or scheduled operations, secret chats or a retained selection as a
source. `addChatToList` and `editMessageText` are the only state-changing TDLib request types added, and
the read `getChatListsToAddChat` the only other one; their closed shapes are listed in the authority
boundary above.

**Reading the chat list.** The current list is read from the chat's positions. Only a `chatListMain` or
`chatListArchive` position whose `source` is null counts (TDLib JSON omits a null `source`); an
MTProxy-sponsored or public-service-announcement position (any `chatSource*`) is never membership, a
malformed `source` makes the positions unavailable, folder positions are ignored, and a second
position for the same list, sponsored or not, makes the positions unavailable. A chat whose only Main/Archive position is sponsored is therefore
`unsupported_chat` at preparation and `chat_changed` at apply, and after the call a sponsored position in
the target list is not an observed move. A secret chat is never accepted.

**Fresh session and offered lists.** A fresh session has not loaded its chats, and `getChat` then answers
400. Only that answer makes the chat read call `loadChats` once for Main and once for Archive (limit
100; a 404 end answer is fine) and repeat `getChat` exactly once; any other failure, a failing load or
a second 400 is `unavailable`. After the "already in the target list" answer, preparation asks
`getChatListsToAddChat` for the chat: if the target list is not among the Main/Archive lists it offers
(folder lists are ignored), the chat is `unsupported_chat` and no draft or dialog exists, so the owner
is never asked to approve a move Telegram refuses (for example the Telegram service chat). A
malformed answer or a failing request is `unavailable`. Apply-time behaviour is unchanged: a 4xx from
Telegram after the attempt is still `failed`.

**Reading the message.** The broker, not the caller, reads the message with one serialized provider
read (chat, `getMessage`, `getMessageProperties`). It must be an outgoing `messageText` that is not a
forward, has no reply markup, no self-destruct, auto-delete, ephemeral, scheduling or sending state, and
for which `can_be_edited` is a strict true. It then goes through the F14 source projection: entities
must be exact records of the visible fieldless allowlist, link-preview options and the link-card URL
must be visible in the text, and an author signature that sanitizing would change refuses. Replacing the
text discards what the owner could not see, so hidden formatting, hidden link targets and buttons refuse
(`hidden_source_content`) instead of being dropped silently. The projection (text, entities, edit date,
link-preview options) is hashed as `source_sha256`, shown to the owner, bound into `preview_sha256`,
compared at apply before the dialog and compared again inside the provider call after approval. A missing
or mistyped safety field is never treated as safe.

**The stored text is the reviewed text.** The pinned TDLib (td/telegram/misc.cpp at d1085f9) cleans
message text and captions before sending with `clean_input_string`, and `fix_formatted_text` then trims
spaces and line feeds (only those) at both ends.
Since the owner's 20.10.2026 decision the shared normalization of every text send, caption and edit
applies the same rewrite before the preview, so the dialog shows exactly the text Telegram stores:

| Input contains | TDLib or Telegram does | Shared normalization (NFKC, CRLF and CR to LF, then the rewrite) |
| --- | --- | --- |
| a space or line feed as the first or last character (after the rewrite, a tab is a space; NFKC turns no-break and ideographic spaces into spaces and U+00A8 into a space plus a mark) | Telegram trims it | trimmed; other edge whitespace such as U+1680 is kept, as in Telegram |
| U+0000 to U+0008, U+000B, U+000C, U+000E to U+001F | replaced by a space | refused as control characters |
| U+0009 (tab) | replaced by a space | replaced by a space |
| U+000D | removed | never present (normalized to LF) |
| U+000A, interior U+0020 | kept | kept |
| U+2028, U+2029 | removed | removed |
| U+030A, U+0333, U+033F | removed | removed, repeating NFKC until stable (U+02DA and U+2017 contain them) |
| U+202A to U+202E | removed | refused as format characters |
| a run of two or more U+200E or U+200F | all but the last become U+200C | refused as format characters |
| more than 35 000 bytes | truncated | impossible (at most 4096 characters) |

A text that is empty after the rewrite is refused. `TextEditSpec`, `TextEditDraftPreview`, the closed
`editMessageText` shape and the send-shape validators still reject any text that is not already in this
form, so no path can send a text the dialog did not show exactly. Before this decision, edits refused
such texts and sends admitted them unchanged; the two edit details for that remain in the schema as
guards.

**Single attempt and result.** The claim is single-use and taken only after owner approval. One request
is sent and never repeated: not after a timeout, a malformed or lost result, a transport error, an
authorization change or a restart. Before transport a refusal is `local_failed`. After it, only a TDLib
error whose code is 400 to 499 is a provider rejection (`failed`, with the code as the only provider
detail); a 5xx, negative, zero, missing or malformed code, a timeout and a transport failure are
`outcome_unknown`, because the change may have been applied. A move is `applied` only when a bounded
`getChat` observation (every 0.25 seconds for at most 10 seconds) shows an own position in the target
list after an `ok` result; any other observation is `outcome_unknown`. An edit is `applied` only when the
returned message has the same ID and chat, `messageText` content whose text equals the new text exactly,
and a non-zero `edit_date` not earlier than the previous one; a result for another message or chat, with
other text or with a stale or zero date is `outcome_unknown`. No provider error string, title or text is
copied into a response. A consumed claim never reads `expired`: lost authority after the attempt is
`local_failed` (no attempt) or `outcome_unknown` ("current receipt authority is unavailable; do not retry
automatically"), and observed state is disclosed only to the same current account and policy.

**Drafts, status and receipts.** Drafts are bound to the proxy client and current account, live in the
same in-memory registry (internal kinds `archive` and `text_edit`) and are not reachable through the
generic draft tools or the send tools, which return `unavailable` or `error` even after the change was
applied, so a change receipt is never replayed as a send. A foreign client, new proxy identity, account
change, broker restart, supersession or expiry returns the content-free `expired`. Preparing again for the
same owner and chat (archive) or owner, chat and message (edit) supersedes the earlier pending draft. The
registry records an `applied` receipt only for these kinds, with the observed list or edit date; status
reads never disclose it and never report `sent`. Receipt retention is 900 seconds from the attempt.

**Hostile content in the dialog.** Chat titles and message texts are untrusted. The MCP preview carries
the texts as marked reversible JSON strings. The local dialogs are written for a human: the chat title
and each covered entity span are enclosed in `« … »`, and the current and new texts are gutter payloads,
under the same reading rule as the forward dialog, so untrusted content can neither close its span nor
start a dialog line. Each dialog is rebuilt from the stored immutable draft and validated against chat
ID, title and hash before display (a mismatch shows no dialog and is `dialog_unavailable`). The archive
dialog states that the chat leaves
its list and loses any pin there, that a chat folder excluding archived chats hides an archived chat,
and Telegram's return-to-Main behaviour; the edit dialog shows the chat kind (a channel edit is visible
to all subscribers), that link-preview settings return to Telegram's default and that recipients see the
message marked "edited", and it lists formatting that is removed apart from entity kinds that Telegram
detects again (Url, EmailAddress, PhoneNumber, Mention, Hashtag, Cashtag, BotCommand, BankCardNumber).

Residual risks, recorded rather than closed:

- **Informational read side effect, closed by requiring `read`.** `prepare_text_edit` reveals the exact
  current text, entities and edit date of an own outgoing text message, and the chat's title and kind;
  `prepare_chat_archive` reveals a chat's sanitized title, kind and Main/Archive membership, including
  through the `already_in_target` and `unsupported_chat` answers. Since 20.10.2026 both capabilities
  also require `read`, so these narrow reads are never available without it.
- **Concurrent applies.** Two concurrent applies of one draft make exactly one provider attempt, but both
  callers may see the dialog, and the caller that lost the claim reads `outcome_unknown` ("current receipt
  authority is unavailable; do not retry automatically") although it made no attempt. This is the shared
  pattern of forward and video sends (an attempt-outcome check routes any consumed claim to "attempted");
  it errs towards unknown, never towards a second attempt or a false `applied`.
- **Plain text sends are rewritten by TDLib the same way** (see above); only edits refuse these characters.
- **Reads are cached or offline provider state.** A remote edit, a deletion or a move from another
  client can race the final check, and Telegram acts on whatever it holds at transport time. The edit
  window can close between preparation and apply; that is `message_changed` (or `local_failed` when it
  closes during the dialog) with no attempt.
- **`applied` is the state right after the call.** Telegram may move an archived chat whose
  notifications are enabled back to Main on a new message, and a move that becomes visible after the
  10-second observation is still `outcome_unknown`. An edit is confirmed by the returned message, not by a
  later readback.
- **An edit is destructive and has no undo.** Formatting, entity targets and link-preview settings of the
  old message are gone, the previous text is not kept, and a later edit cannot restore them; recipients see
  the message marked "edited". The classification of entities as removed or detected again is the broker's
  expectation of Telegram behaviour, not a provider fact.
- Real owner-approved archive and unarchive of a test chat and a real edit of an own test message have not
  been run; the evidence is a synthetic provider and dialog. The same-UID cooperative client boundary is
  unchanged, with no hostile same-UID secrecy claim.
