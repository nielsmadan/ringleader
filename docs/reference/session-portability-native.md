# Native session portability profiles

Measured on 2026-09-22. These are deep portability adapters, separate from the
cheap discovery `Source` implementations. Staging returns owned bytes and writes
nothing. Native publication and CLI commands are implemented separately.

## Roots and subprocess contracts

`NativeRoots.store` is the agent root: Claude's `.claude` (containing `projects/`)
or Pi's `.pi/agent` (containing `sessions/`). Every `NativeFile.relative_path` and
`NativeStage.source_ref` is relative to that root. Source requests also accept an
explicit absolute transcript path, including an archived copy.

`home` supplies `HOME`; `config` and `cache` supply `XDG_CONFIG_HOME` and
`XDG_CACHE_HOME`. `cwd` is the destination project directory. Production callers
should provide absolute paths. Claude project-directory encoding resolves symlinks
and normalizes Unicode to NFC, matching the pinned SDK. Cwds whose sanitized name
exceeds 200 characters fail because the CLI/SDK hash variants are not verified.

`RunRequest.env` contains overrides, not a complete process environment. A production
runner applies them to the user's environment, retaining authentication and PATH.
The overrides also place XDG data/state beneath `home` and TMPDIR beneath `cache`.
Production resume passes only the session selector and cwd, allowing the native
agent to load current destination project instructions. Version preflight uses
separate disposable roots, never the destination store. The smoke runner uses a
complete allowlisted environment with PATH, locale, disposable roots, and its
explicit probe inputs; it inherits no authentication, native settings, hooks, or
extensions. There are no provider requests.

## Claude Code

Writer profile: `claude-code-2.1.278`, exact CLI output `2.1.278 (Claude Code)`.
The reader supports the verified JSONL parent graph independently of installed CLI
version. The official reference is
[`claude-agent-sdk-python` v0.2.156](https://github.com/anthropics/claude-agent-sdk-python/tree/v0.2.156),
particularly `_internal/sessions.py` and `_internal/session_mutations.py`.

The reader selects a main-chain terminal, walks UUID parents, excludes sidechains,
and follows compact boundaries' logical parents to retain historical messages.
The active context starts at the latest boundary summary. Progress and identified
telemetry do not become messages. Meta messages stay in context, outside visible
history; their combination with compaction currently fails explicitly. Branch IDs
can select an earlier or alternate valid chain. Duplicate IDs, forward/missing
parents, cycles, incomplete tools, and unknown conversational payloads fail.

The writer emits fresh session/message IDs, current session metadata, a tail
`custom-title`, zero assistant usage, matched native `tool_use`/`tool_result`
blocks, and embedded base64 images in user/tool-result content. Assistant image
blocks are rejected in both reader and writer: the pinned SDK v0.2.156
`types.py` assistant `ContentBlock` union contains text, thinking, and tool blocks,
with no image member. Actual Claude CLI context remains unprobed.

Historical message times remain in each native message row. A leading
`custom-title` metadata record carries the current import time; the SDK therefore
reports fresh creation and filesystem recency without inventing a conversation
message or changing a historical turn. Compaction summaries and retained context are
real native messages on the active parent chain. Original historical records remain
reachable through `logicalParentUuid`. Retained historical copies are checked
against their referenced native history before restoring canonical references.

Nonempty `content-replacement` records, unknown attachment-record payloads,
unverified retained-segment compact metadata, transcript-only messages, and
unsupported system payloads fail explicitly. No replacement schema is inferred
from the SDK merely copying replacement records. Tools require JSON object inputs.
Supported image media types are PNG, JPEG, GIF, and WebP; external image URLs and
other attachment types fail. An exact newer CLI version needs another verified
writer profile.

## Pi

Writer profile: `pi-0.85.1-v3`, exact CLI output `0.85.1`.
References are the installed pinned package
`@earendil-works/pi-coding-agent@0.85.1`, `docs/session-format.md`,
`dist/core/session-manager.js`, `dist/core/messages.js`, and `dist/cli/args.js`.

The reader requires a v3 header, follows the selected entry's `parentId` chain,
and defaults to the last persisted entry. Extension state entries do not enter
context; `custom_message` and branch summaries do. A custom message with
`display=false` stays context-only. The canonical model cannot currently combine
that visibility split with a checkpoint, so that case fails explicitly. Both
`firstKeptEntryId` and documented embedded `retainedTail` checkpoints are readable.

The installed runtime's `buildContextEntries` and `sessionEntryToContextMessages`
ignore `retainedTail`, despite the package documentation describing it. The smoke
reproduces this discrepancy. The writer therefore emits only `firstKeptEntryId`
checkpoints with contiguous retained suffixes, and requires Pi's literal native
summary wrapper to preserve the exact active context. Embedded retained context
cannot be written to this profile. Empty retained suffixes point at the checkpoint
itself, selecting no earlier entries in the native builder.

The writer emits v3 JSONL beneath the native cwd directory, current session
creation metadata, preserved historical message/entry timestamps, zero usage, a tail `session_info` name, native assistant tool calls, individual
`toolResult` messages with error flags, and embedded images. Historical assistant
provider/model/API fields use accepted placeholders (`anthropic`,
`claude-sonnet-4-5`, `anthropic-messages`); they are not imported usage claims.
Resume uses `pi --session <id>`; `--resume` only opens the picker.

Unsupported content fails before staging: freeform/non-object tool arguments,
multiple results or mixed results/prose within one canonical message, shell
`bashExecution` messages with either `excludeFromContext` value, assistant
`errorMessage` payloads, assistant images, aborted/error/pending assistant states,
missing or unknown assistant stop reasons, interrupted pending tool exchanges,
unknown roles/blocks, non-image attachments, and visibility
splits that would promote hidden context into visible history.

Pi tool results must follow their calling assistant contiguously. Prose within
that assistant and several contiguous results are supported. An intervening user
or assistant would cause the native provider transform to insert synthetic error
results, so both source reading and target staging reject that sequence. Native
aborted/error assistants are similarly excluded by the provider transform and
cannot be imported as ordinary completed context.

Pi's installed picker calculates `modified` from the latest historical
user/assistant message time, ignoring fresh header and metadata times whenever
messages exist. Imported sessions therefore have fresh creation/filesystem times
but retain historical native picker sorting. This limitation is intentional
(ruling R8): no old turn is redated and no synthetic conversation message is
inserted to force recent sorting.

## Historical annotations

Per-message `ringleader` annotations preserve provenance, historical timestamps,
and canonical roles. Historical `Instruction` and explicit tool-omission markers
are rendered in native text; the reader verifies that text before recovering the
typed annotation. A historical system/developer instruction is a labeled native
user message, never fresh target system policy. Ordinary prose and tools are read
from native content; annotations contain no substitute conversation replay.
Session annotations retain source branch identity and original source timestamps.
Historical timezone-bearing ISO times and Pi/OpenCode epoch milliseconds are also
written into native message timestamps. Unrepresentable historical values fail
instead of silently becoming import time. Messages without source times use the
current time.
Attachment names accompany target-owned image bytes. The only comparison
normalization is JSON tool-argument whitespace.

## Repeatable native verification

Install the pinned SDK into a disposable validation directory (no project runtime
dependency), then run from this checkout:

```sh
uv pip install --target "$validation_dir" claude-agent-sdk==0.2.156
uv run python scripts/smoke-portability-native.py \
  --claude-sdk "$validation_dir" \
  --pi-module "$pi_package/dist/core/session-manager.js"
```

Optional `--claude`, `--pi`, and `--node` arguments select executable paths.
All native probes run on disposable copies, with isolated HOME, native roots,
XDG directories, TMPDIR, and cwd. The isolated source store is deleted before
probing target bytes. The smoke asserts that native reads did not change those
bytes. It verifies plain and checkpoint sessions separately.

Measured result: both installed CLIs passed version/help probes. The pinned
Claude SDK independently found both picker titles and read each four-message
active visible chain, including actual tool calls/results, errors, and image bytes.
Its chain reader deliberately excludes the earlier logical-parent history.
This is **not proof of the Claude CLI's resumed model context**; that probe remains
unrun. Pi's installed `SessionManager` independently listed both targets, read all
four historical messages, and rebuilt each four-message model context through
`convertToLlm`; tools, arguments, result pairing, errors, images, ordering, cwd,
name, and zero usage all passed. Its argument parser accepted `--session <id>`.
The extended smoke also executes Pi's installed `transformMessages` and captures
the installed Anthropic provider's actual outbound payload through `onPayload`,
with an injected mock client and an exception before transport. Both supported
histories preserve their text, tools, errors, images, and order in that payload.
Negative probes reproduce the dropped aborted/error turns, discarded assistant
images, and inserted synthetic results for interrupted tool exchanges; Python
regressions assert those inputs now fail. Multiple contiguous results remain
supported. Historical 2020 timestamps survive both writers; the SDK verifies
fresh Claude creation/recency, and Pi verifies fresh creation with historical
picker modified time. No network transport, native inference, or tool execution
was performed.

## Codex

Writer profile: `codex-0.155.1-legacy`, exact CLI output `codex-cli 0.155.1`.
The reader recognizes legacy JSONL independently of the installed executable.
References are pinned to [Codex rust-v0.155.1](https://github.com/openai/codex/tree/rust-v0.155.1/codex-rs):
`protocol/src/{models,protocol,dynamic_tools,items}.rs`,
`app-server/src/request_processors.rs`, `app-server-protocol/src/protocol/thread_history.rs`,
`rollout/src/{policy,session_index,recorder}.rs`, and `state/migrations/`.
`NativeRoots.store` is `CODEX_HOME`; resume is `codex resume <uuid>` from destination cwd.

Visible `event_msg` history and model `response_item` context are independent.
Function/custom call kinds, arguments, paired full outputs, embedded image bytes,
historical message times, task boundaries, and explicit compaction
`replacement_history` are preserved. User text containing `<INSTRUCTIONS>` is
ordinary user content. Ambiguous nonempty `SessionMeta.base_instructions` becomes
a historical context-only `Instruction` with native provenance stating that the
metadata does not identify its author/default origin. The target's base instructions
remain unset, allowing current destination policy and project instructions to load.
Claude/Pi currently reject this context-only visibility split.

Compactions with separate visible/model suffixes use optional canonical
`Checkpoint.following_context`. Its content must match the active context after
summary and retained context. Older serialized checkpoints default this field to
`None`; all other unknown/missing fields retain strict decoding. Compact conversion
selects visible post-boundary messages by their historical position, preserving
prose even when model IDs/content/provenance differ. Claude/Pi explicitly reject
this independent post-checkpoint suffix; Codex writes it as native model items.

Native cold-read testing found a critical distinction: the generic history builder
understands dynamic tools, but the app-server's legacy API filters those events
through persistence policy. `DynamicToolCallRequest` and `DynamicToolCallResponse`
are transient and disappear from `thread/read`. Historical tool display therefore
uses durable `mcp_tool_call_end` events under the synthetic server name
`ringleader_history`, with the original tool name, arguments, complete text/image
output and success state. Matching call/result events update one native UI item.
The adapter verifies their identical native payloads before recovering the
canonical call/result grouping. No server is configured and no tool is executed.
Model items remain native matched function/custom calls and outputs.
Visible and model tool maps stay separate, including MCP display names such as
`read` paired with the model name `mcp__files__read`. Source MCP server identity
is retained as historical provenance.

The source reader also recognizes complete native exec-command begin/end records,
patch completion records, and durable `item_completed` `FunctionCallOutput` items.
Exec stdout, stderr, aggregated and formatted output remain exact strings in labeled
native-output blocks. Patch output retains exact stdout/stderr and all recognized
Add/Delete/Update fields, including move paths, in a labeled JSON block. Command
metadata stays in provenance; independently truncated model output stays separate.
Completed function outputs require an unambiguous matching response output ID and
tool name. Unknown patch fields and ambiguous correlations fail explicitly.
Pinned `rollout/src/policy.rs` marks exec begin/end transient, so 0.155.1's cold API
omits them even when present in an older rollout. Its cold API does expose durable
patch completions and completed function outputs. The reader preserves supported
legacy exec records independently of that installed display policy; writing converts
all three historical families to the durable MCP display described above.

Codex serializes model tool output bodies without a success boolean. Each written
model result therefore starts with an explicit native text block,
`[Historical tool result: succeeded]` or `[Historical tool result: failed]`, followed
by the untouched original output blocks. Readback verifies that text before
recovering the canonical error flag. Native loopback request captures prove both
markers survive, including success/failure results with identical original output.
Annotations never substitute for missing native text, tool data or images.

Unsupported shapes fail explicitly: paginated/inherited/compressed histories,
unknown required rows/events/response blocks, incomplete tasks/tool pairs,
non-image/audio/remote/local-path attachments, non-default image-detail hints,
assistant images, namespaced model tools, MCP structured content, and authored
instruction overrides present only in `turn_context`. Nonempty assistant message
phases in either stream and visible `local_audio` also fail before canonicalization.
Metadata/accounting and
structurally tagged hidden reasoning are excluded. Visible tool families beyond
MCP, complete exec, patch and matched completed function outputs fail explicitly.
Empty canonical messages and visible images without preceding
user text are also unsupported.

### Registration and recovery boundary

The adapter requires an initialized native home with completed startup backfill.
It gates `state_5.sqlite` against the exact measured schema (including tables,
indexes and triggers) and all 55 successful migration checksums. The fixture
`tests/portability/codex-state-5.json` contains that independently generated native
schema/migration identity. Unsupported versions/schemas, missing databases and
configured `sqlite_home`/`CODEX_SQLITE_HOME` overrides fail before staging. Version
probes and schema inspection use disposable roots/copies; read-only preflight does
not create SQLite WAL sidecars in the destination. The final registration rechecks
schema under `BEGIN IMMEDIATE`, inserts only the new thread, and enforces ID/path
uniqueness without replacing the DB or migrating it.

Task 6's journal must record three external intents in order:

1. `Adapter.index.index_intent(stage)` describes the append-only native name row;
   `append_index(stage)` writes and fsyncs it. It must precede rollout visibility.
   Retries recognize the original row and preserve later native/user renames.
   An existing conflicting name or incomplete index record fails; the whole shared
   index is never rewritten or truncated.
   Before creating/appending the index, a SQLite write transaction gates the schema
   and checks thread identity, while both native rollout directories are checked
   for that UUID. An unnamed existing thread also fails without index mutation.
   Retry ownership requires the original index intent plus matching rollout bytes
   and, when registered, immutable DB identity fields.
2. Create-only publication of the staged rollout. Its `visibility=True` marks a
   forward-recovery boundary: native filesystem fallback can discover it already.
3. `Adapter.registration.register_stage(stage)` inserts the staged database payload.
   `recover=True` recognizes a matching row only with original immutable identity
   fields and the staged transcript as an unchanged prefix. Native append/resume
   and path canonicalization are tolerated; collisions or replaced content fail.

Session creation/recency and native name-index times are current. Historical row
and turn timestamps remain historical; usage is zero. File publication and the two
indexes cannot have one atomic visibility point. These are narrow publication
primitives; the durable operation journal remains the conversion service's job.

### Repeatable Codex verification

```sh
uv run python scripts/smoke-portability-codex.py --evidence /absolute/new/evidence-dir
```

The script initializes disposable homes and waits for backfill before import. It
first measures native parsing of exec, patch and completed-function source fixtures,
including the transient-exec distinction above. Unit fixtures cover successful and
failed exec/patch output, empty patch stdout, all four file-change cases, compact
conversion and native roundtrip.
It then
proves DB registration, native list/read with cwd/source filtering, full ordered
visible messages/tool names/arguments/outputs/errors/images, titles, cold restart,
and resume. Resume happens in separate disposable sessions. Five actual native
outbound model requests are captured by a loopback HTTP server returning an error
before inference: plain history, retained-tail compaction, independent model suffix,
identical success/failure output, and distinct MCP display/model names. They retain model call/output pairing, full
output/image bytes, status markers and destination `AGENTS.md` instructions.
Original staged rollout bytes remain unchanged by the cold-read probes. Unit tests
also delete the isolated source rollout after snapshot and retain its image bytes.
No inherited credentials, hooks, extensions, external inference or tool execution
are used. The run validates app-server views/model requests; interactive TUI
rendering is not separately exercised.
