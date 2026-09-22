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
extensions. Actual Claude/Codex request capture terminates at a loopback HTTP mock
before inference; Pi intercepts the provider payload before transport. No external
provider requests or real tool execution occur.

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
with no image member. Actual Claude 2.1.278 bare-mode request capture independently
establishes supported resumed history. Failed tool-result images are stripped by
the native CLI; reader and writer explicitly reject that shape. Successful result
images and textual failures survive. This distinction holds with and without a
compaction checkpoint. Bare mode disables automatic CLAUDE.md discovery, so it does
not establish normal-mode project instruction loading.

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
Claude attachment names live in per-row `ringleaderAttachments` annotations,
separate from provider `message.content`. Each annotation binds its positional
image to the SHA-256 of the actual native bytes and its MIME type; readback verifies
both before restoring the name. Native provider image blocks contain only `type`
and `source`, never `ringleaderName`. Pi retains its native image annotation, while
its independently captured provider transform reconstructs clean image blocks.
The only content normalization is JSON tool-argument whitespace; regenerated
message IDs/parent references do not change ordered message semantics.

## Repeatable native verification

Install the pinned SDK into a disposable validation directory (no project runtime
dependency), then run from this checkout. The Pi path below is resolved from the
installed package layout used by the measured 0.85.1 npm installation:

```sh
validation_root="$(mktemp -d "${TMPDIR:-/tmp}/rl-native.XXXXXX")"
pi_package="$(python3 -c 'from pathlib import Path; import shutil; print(Path(shutil.which("pi")).resolve().parent.parent / "@earendil-works/pi-coding-agent")')"
claude_executable="$(python3 -c 'from pathlib import Path; import shutil; print(Path(shutil.which("claude")).resolve())')"
uv pip install --target "$validation_root/sdk" claude-agent-sdk==0.2.156
uv run python scripts/smoke-portability-native.py \
  --claude "$claude_executable" \
  --claude-sdk "$validation_root/sdk" \
  --pi-module "$pi_package/dist/core/session-manager.js" \
  --evidence "$validation_root/evidence"
```

Optional `--pi` and `--node` arguments select executable paths. The actual installed
Claude binary is used, without an authentication wrapper. The harness validates
both versions/help, deletes the isolated source store before probing target bytes,
and preserves frozen stage originals. Native sessions run on separate disposable
copies with isolated HOME/native/XDG/config/cache/state/TMP/cwd and an allowlisted
environment. The evidence directory contains synthetic request captures and
stdout/stderr; remove that disposable directory when no longer needed.

Measured result, 2026-09-22: the pinned Claude SDK finds picker titles and reads
four-message plain/checkpoint chains. Six actual Claude CLI `/v1/messages`
requests cover both forms with successful tool images, textual failures, and a
deliberately modified failed-image negative fixture. The harness asserts ordered
roles/text, instructions, tool names/arguments/pairing/error flags and exact image
bytes/MIME. Positive provider image blocks have native schema fields only.
Negative fixtures prove the CLI drops failed-result images and the adapter refuses
them. Each request receives HTTP400 from the local mock; subprocess exit1 is
expected capture completion, not API success or model inference.

Isolation uses `--bare`, `CLAUDE_CODE_SIMPLE=1`, disabled nonessential traffic and
official marketplace auto-install, loopback `ANTHROPIC_BASE_URL`, a fake API key,
empty tools/MCP/settings sources, disabled slash commands, no Chrome, and disabled
hooks. No inherited authentication, extensions, hooks or project files are loaded.
Bare-mode history reconstruction is established; normal-mode project instruction
loading and interactive TUI rendering remain unverified.

Pi's installed SessionManager lists targets, reads full history and rebuilds
context through `convertToLlm`. `transformMessages` and the installed Anthropic
provider's `onPayload` callback independently capture full/checkpoint payloads,
throwing before transport. Text/tools/arguments/pairs/errors/images/order and native
launch arguments pass, including both successful and failed-result images.
Negative probes reproduce ignored embedded `retainedTail`, dropped aborted/error
turns, discarded assistant images, and synthetic results after interrupted tools.
Historical timestamps and the Pi picker recency limitation are asserted separately.

## Acceptance fixtures and public CLI recovery procedure

`tests/portability/test_matrix.py` contains two separate full/compact 4×4 matrices.
The shared-content matrix uses generated native stores, service publication and
readback, repeat reuse, and all same-agent forks; it is roundtrip evidence. The
independent matrix uses ordinary native-shaped Claude/Pi tool-error histories,
OpenCode native tool graphs, and Codex native exec display/model output with
ambiguous base instructions. It asserts exact refusals where these semantics
cannot be written. Existing adapter fixtures cover native compaction and distinct
post-checkpoint streams. None of these tests substitutes for native runtime proof.

For reproducible public CLI QA, use the source checkout entrypoint
`.venv/bin/python -m ringleader` in a subprocess with an allowlisted environment.
Create a disposable HOME/XDG/config/cache/state/TMP/cwd and pass explicit
`--registry` and `--operations` paths. `tests.portability.test_cli.seed` creates
owned fixture transcripts/registry rows; `test_service.source` and `service`
provide actual adapters and isolated state. For launch construction tests only,
put recording executables on that subprocess's PATH: `--version` returns the
pinned version; resume arguments write argv/cwd into a disposable evidence file.
Label these as launch-plumbing tests, never native acceptance.

Compare the entire disposable tree before/after `convert --dry-run`. Then run
convert/repeat/fork/compact/lineage in separate processes; remove source bytes and
read back target attachments. Exercise a valid+missing-source batch twice, and
archive/delete owned results through the public commands before repeating convert.
Hold `operations/operations.lock` with an exclusive `flock`, start a CLI fork,
observe it pending, mutate an unrelated registry field through `mutate_registry`,
release the lock, and verify both updates.

To reproduce crash recovery without changing product code, construct the fixture
service with `fault=crash_at("after:file:0:action")`, catch the test `Crash`, then
invoke public `data transaction resume` on its journal from a fresh process. A
second `after:create` fixture exercises private rollback. A published transaction
must refuse rollback. Capture exit/stdout/stderr plus native/registry state for
each command; release locks, wait for owned processes, and delete disposable
stores afterward. These fault hooks are Python test inputs, not CLI flags.

## Codex

Writer profile: `codex-0.155.1-legacy`, exact CLI output `codex-cli 0.155.1`.
The reader recognizes legacy JSONL independently of the installed executable.
References are pinned to [Codex rust-v0.155.1](https://github.com/openai/codex/tree/rust-v0.155.1/codex-rs):
`protocol/src/{models,protocol,dynamic_tools,items}.rs`,
`app-server/src/request_processors.rs`, `app-server-protocol/src/protocol/thread_history.rs`,
`rollout/src/{policy,session_index,recorder}.rs`, and `state/migrations/`.
`NativeRoots.store` is `CODEX_HOME`; resume is `codex resume <uuid>` from destination cwd.
Source snapshots read the rollout and native `session_index.jsonl` together under
stability checks. The latest matching name row overrides transcript annotations;
that selected title participates in reuse identity. Unrelated name-index changes
do not invalidate an otherwise unchanged source snapshot.

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

The service journal records three external intents in order:

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
The independent oracle compares the complete ordered historical model prefix,
including all user/assistant text, instruction labels, image MIME types and decoded
bytes, tool names, arguments, pair IDs and success/failure markers. It separately
validates the fresh destination-policy/environment/request suffix. Visible history
receives the same content/order checks through the native read API. Mutation tests
use captured historical fixtures (with a sanitized bootstrap/environment suffix)
to prove corrupted or missing content fails both oracles.
Original staged rollout bytes remain unchanged by the cold-read probes. Unit tests
also delete the isolated source rollout after snapshot and retain its image bytes.
No inherited credentials, hooks, extensions, external inference or tool execution
are used. The run validates app-server views/model requests; interactive TUI
rendering is not separately exercised.

## OpenCode 1.18.30 (source/schema verified; native execution unverified)

The `opencode-1.18.30-v1` adapter targets the legacy `session`/`message`/`part`
graph in an already initialized native `opencode.db`. The pinned sources are
[the v1 schema](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/schema/src/v1/session.ts),
[the model-context builder](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/session/message-v2.ts),
[generated SQL](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/core/src/database/schema.gen.ts),
[the migration journal](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/core/src/database/migration.gen.ts),
and [native project resolution](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/core/src/project.ts).
The tool state schema declares `status: Schema.Literal("error")`; the adapter
preserves these completed error results as `ToolResult.is_error=True` with their
full error strings. Pending/running tools are incomplete and rejected.

The exact fresh native schema fixture in
`tests/portability/opencode-1.18.30.json` contains all 37 generated CREATE statements
(including the native migration table) and all 38 migration IDs. The writer gate
hashes the full SQLite schema, normalizing SQL whitespace, and the sorted migration
IDs. The supported identity is
`opencode-1.18.30:4e1c29893bede253e37904f6e62e09cd5c82bc37da02c7c147f665c6bd1dbbf9`.
An upgraded database with extra historical migration tables, another channel's
schema, missing migrations, or unknown tables fails this strict profile. The
adapter never initializes or migrates a live database. Source compatibility uses
this pinned graph/schema profile independently of the installed executable version.

`NativeRoots.store` is the native `opencode` data directory, and `source_ref` is
`opencode.db`. The source reader first takes a stable bounded DB/WAL copy, then uses one explicit
SQLite read transaction across
the selected session, project reference, messages, parts, and V2 presence checks.
It checks native ownership and parent references and snapshots local file bytes
with bounded identity/change checks while that transaction remains open. The
result owns attachment bytes. Target staging embeds supported images/PDFs as data
URLs, so removing the source store does not break the new history. Schema
preflight reads stable copies of the database and WAL in temporary storage, so it
does not create source or target WAL/SHM sidecars. Staging is pure and returns an opaque DB
payload, with no published files.

Supported shared content includes nonempty ordered user/assistant prose,
historical instructions explicitly labeled as history, completed JSON-object tool
calls followed by one contiguous result message in call order, successful outputs
with images/PDFs, and completed error outputs without attachments. Tool results
require one text block followed by any attachments. Full output strings are never
truncated. Source reasoning and recognized accounting/snapshot/patch telemetry are
excluded; unknown conversational fields fail. Native text marked `synthetic` is
preserved as authored text unless a structural native compaction part identifies
its role. Original timestamps and provenance survive in verified structural
annotations, while new sessions have fresh sortable native IDs, current creation
and recency, and zero fabricated usage.

Native tool parts store a call and result together. The reader represents their
ordered calls and matching results as assistant/tool messages; the writer checks
this grouping against actual native parts. Session metadata carries only the
original provenance, timestamps, historical instruction/omission labels and
structural message boundaries. It does not contain a canonical replay copy that
could conceal missing native output. Mutating a labeled historical instruction or
an annotated native timestamp fails readback. The writer compares full canonical
history and active context before returning a stage.

Native compaction reading preserves the mandatory user context prompt and assistant
summary, then reorders the retained tail for model context. The pinned builder's
literal prompt is `What did we do so far?`. Writing a checkpoint requires that
native question/summary pair to already exist adjacently in visible history and
its retained tail to be a contiguous preceding suffix. Arbitrary summaries,
independent retained context, and `following_context` cannot be represented by this
writer and fail explicitly. Plain deterministic compact histories roundtrip;
compact transforms that produce an independent checkpoint require another target.

The following native states also fail specifically before publication: session
revert; active V2 `session_message`, `session_input`, or `session_context_epoch`
rows; ongoing compaction; incomplete assistant turns; assistant errors/aborts;
ignored text; empty conversational text; tool `time.compacted`; interrupted tool
metadata; any other nonempty tool-state metadata; provider-sensitive part metadata; user `system` instructions; structured
output; subtask/agent/retry parts; prose after tools within one native message;
assistant file parts; text-file/directory transformations; remote attachments; and
non-embedded tool attachments. Nonempty tool-state metadata is rejected because
the native UI can render fallback shell output, edit diffs/diagnostics, patch files
and question answers from it, independently of model output. No metadata key is
currently allowlisted as irrelevant. This distinguishes ordinary source limitations from
the smaller shared-supported conversion matrix. Attachment placement in actual
model requests is provider dependent: the pinned source retains media in tool
results for Anthropic/OpenAI, while other providers may extract a synthetic user
media message. That runtime behavior has not been measured here.

Project IDs follow native resolution: normalized origin remote hashed with SHA-1,
then the existing common-directory `opencode` cache, then the lexically first root
commit, then `global`. Non-git projects use the filesystem root as worktree.
Registration resolves again and checks the staged identity; it never writes
`.git/opencode`. A single `BEGIN IMMEDIATE` transaction inserts only the new
project (when absent), session, messages and parts. Primary keys, ownership checks,
foreign keys and the pinned schema guard prevent merging into existing graphs.
A random ownership marker in session metadata plus immutable identity fields
provides recovery evidence. Recovery acknowledges a graph already committed by
this operation and leaves subsequent native renames, message edits and appended
turns intact. A collision or missing marker fails; no rows are replaced or deleted.
The optional adapter identity allocator supplies `ses_` IDs, and message/part IDs
use the native sortable 12-hex/14-alphanumeric suffix shape.

Target continuation requires an explicit native `provider/model`. The pinned
prompt implementation says `if (current?.model)`, then selects that session model
before falling back to historical user-message models. Accordingly, the new session
and native user messages carry the resolved destination model. Historical assistant
metadata is explicitly marked `ringleader-history`, never selected as fresh policy.
Supported configuration precedence is `$XDG_CONFIG_HOME/opencode/config.json`, then
`opencode.json`, then `opencode.jsonc`; all three must contain strict JSON when
present. Actual JSONC comments, environment/file interpolation in the model,
`OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`, `OPENCODE_CONFIG_DIR`, automatic model
discovery and any project `opencode.json`/`opencode.jsonc` or matching `.opencode/` config
are unsupported, even when a project file is empty or contains no model. Home `.opencode` JSON/JSONC
config, any `default_agent` key, nonempty global `agent`/`mode` definitions, and
discovered `agent`/`agents`/`mode`/`modes` directories under native global, home or
project config roots also fail. These definitions can select a different model
before the stored session model is considered.
`Compatibility.target_configuration` exposes the resolved provider/model;
`TargetRequest.target_configuration` binds that inspection and makes configuration
drift fail during staging. Resolution is scoped to each target's roots/cwd.
Launch is `opencode --session <new-id>` in the destination cwd, with explicit
HOME/XDG roots and `OPENCODE_DB` selecting the staged native database.

The installed executable at `/Users/nielsmadan/.opencode/bin/opencode` was denied
read access; `nono why --self` reported `path_not_granted`. Its version is unknown.
No alternative executable was downloaded, copied or relocated. Unit tests and
schema/source inspection establish the supported implementation, **not native
runtime acceptance**. The following harness is provided for an authorized session
with OpenCode **exactly 1.18.30**, Bun, and a clean official v1.18.30 source checkout
whose dependencies are already installed:

```sh
uv run python scripts/smoke-portability-opencode.py \
  --executable /absolute/path/to/opencode \
  --native-source /absolute/path/to/official-opencode-v1.18.30 \
  --evidence /absolute/new/opencode-evidence
```

It uses disposable HOME, XDG config/data/cache/state, TMP and cwd, with an allowlisted
environment, no inherited credentials/plugins/hooks, disabled updates/model fetch,
and no provider calls. Import/export/list validate native schema acceptance and
cold discovery in separate CLI processes; a separate Bun invocation calls the
pinned native `filterCompacted`/`toModelMessages` directly, with network fetch
explicitly disabled. Its assertion compares the complete ordered model-message
projection: roles, historical instruction and omission text, tool names/arguments,
call/result identity, status and full outputs, user and successful-tool media
bytes/MIME, and checkpoint exclusions/retained-tail order. The three cases are full,
plain compact, and native retained-tail compaction. Only model-schema representation
normalizations are accepted: text string versus one text block, embedded base64
versus data URLs, and native omission of an empty text block when tool media exists.
Native media filenames remain verified in import/export graph comparison; the
context builder drops tool-media filenames. The harness also checks transactional registration through
native export. Native import is used only on these disposable validation copies:
its multi-commit/upsert behavior is unsuitable for final publication. The harness
has only passed argument/compile checks here; native execution, TUI rendering and
actual resumed provider request construction remain unverified.
