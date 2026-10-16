# Native session portability profiles

Measured on 2026-09-22, extended 2026-10-08 with self-verification and Droid. These are deep
portability adapters, separate from the cheap discovery `Source` implementations. Staging
returns owned bytes and writes nothing. Native publication and CLI commands are implemented
separately.

## Self-verification

Each adapter accepts an installed agent version against two sources: a short list pinned from
upstream source inspection (`PINNED` in the adapter module; empty for Droid, which pins nothing
and is entirely self-verify-driven), or a version recorded in a local `verified-targets.json`
after an offline harness pass. `rl doctor --verify-conversion [AGENT]` runs the harness directly;
`rl resume --to` runs it itself, interactively only, the first time it needs an unverified
target — never silently, never in a script or `--dry-run`. `--verified PATH` isolates the state
file, the same pattern as `--registry`/`--operations`.

The harness is one of two tiers:

- **model-request-capture** (Claude Code, Codex, Pi, Droid) — stages a small representative
  conversation into the agent's own native format, launches the real installed CLI against a
  loopback HTTP mock standing in for the model provider, and asserts the captured outbound
  request: roles, text, tool pairing/arguments/error flags, image bytes/MIME, native summary
  wrappers, and launch argv. No external inference, no real tool execution, no inherited
  credentials. Droid's harness drives this through `droid exec --fork`, never interactive
  `droid`, so it is safe to run unattended.
- **cli-acceptance** (OpenCode) — asserts native CLI import/export/list acceptance and the
  structural database schema identity. It does not capture OpenCode's own outbound model
  request, which needs a source checkout to invoke the pinned context builder directly (see the
  OpenCode section's separate `--native-source` harness); native runtime request construction
  therefore remains unverified by self-verification alone.

A stored `VerificationRecord` names the agent, exact version string, that adapter's current
`HARNESS_VERSION`, the tier, the timestamp, and (for Codex and OpenCode, both schema-gated) the
schema identity, plus a digest of every `ringleader/portability` source file (the harnesses,
their fixtures and every writer they drive). A harness-logic change bumps `HARNESS_VERSION`; a
record under an older value, or made by different conversion code, is ignored, so a changed check
or writer always re-verifies rather than trusting a stale pass. A failed `rl doctor
--verify-conversion` copies its scratch tree (symlinks kept as links) to `verify-failures/`
beside the state file and keeps the newest three per agent. Every harness
requires its working directory outside any git checkout — Codex loads every `AGENTS.md` up to the
git root, which breaks destination-instruction assertions — and runs with disposable, isolated
HOME/XDG/config/cache/state/TMP/cwd roots and an allowlisted environment: no inherited
authentication, hooks, extensions, or project files.

Measured 2026-10-08 on this machine: Claude Code 2.1.294, Codex 0.161.0, Pi 1.0.1,
OpenCode 1.18.33, and Droid 0.228.0 each self-verify (none matched a pinned version).

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
Production resume passes only the session selector and cwd (plus `--model` for a converted Claude session, below),
allowing the native agent to load current destination project instructions. Version preflight uses
separate disposable roots, never the destination store. The smoke runner uses a
complete allowlisted environment with PATH, locale, disposable roots, and its
explicit probe inputs; it inherits no authentication, native settings, hooks, or
extensions. Actual Claude/Codex request capture terminates at a loopback HTTP mock
before inference; Pi intercepts the provider payload before transport. No external
provider requests or real tool execution occur.

## Claude Code

Self-verified writer: pinned known-good versions `2.1.278` and `2.1.283`; any other installed
version (2.1.294, measured here) self-verifies via `rl doctor --verify-conversion claude-code`
before conversion — see Self-verification above. The reader supports the verified JSONL parent
graph independently of installed CLI version. The official reference is
[`claude-agent-sdk-python` v0.2.156](https://github.com/anthropics/claude-agent-sdk-python/tree/v0.2.156),
particularly `_internal/sessions.py` and `_internal/session_mutations.py`.

The reader selects a main-chain terminal, walks UUID parents, excludes sidechains,
and follows compact boundaries' logical parents to retain historical messages.
The active context starts at the latest boundary summary. Progress and identified
telemetry do not become messages. Meta messages stay in context, outside visible
history; their combination with compaction currently fails explicitly. Branch IDs
can select an earlier or alternate valid chain. Duplicate IDs, forward/missing
parents, cycles, incomplete tools, and unknown conversational payloads fail.

Every written assistant row carries the placeholder model `claude-sonnet-4-5`, and Claude
2.1.301 resumes on the last assistant row's model over a configured default — measured
offline against a loopback capture, in both `--print` and the interactive TUI: a settings
`model` that names a non-default model (`haiku`) wins over the row, but one that names the
default (`opus`), or no setting at all, leaves the session on `claude-sonnet-4-5`; only
`ANTHROPIC_MODEL` and `--model` reliably win. A settings file's `env.ANTHROPIC_MODEL` beats the
shell's, which beats any settings `model`. So when the transcript's last main-chain assistant
row (skipping `<synthetic>` error rows) is one ringleader wrote, launch is
`claude --resume <id> --model <m>`, where `<m>` is the first `env.ANTHROPIC_MODEL` across managed
settings, `<cwd>/.claude/settings.local.json`, `<cwd>/.claude/settings.json` and
`$CLAUDE_CONFIG_DIR/settings.json`, else the shell's `ANTHROPIC_MODEL`, else the first `model` in
those files, else `default`. A native session, or a converted one Claude has since answered in,
launches as plain `claude --resume <id>` and keeps its own model.

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
other attachment types fail. A newer CLI version self-verifies rather than needing
another pinned writer profile — see Self-verification above.

Each attachment row is classified by its own kind against 2.1.294's rendering: a
fixed catalog of kinds (`queued_command`, `poll_events`, `hook_success`/
`hook_additional_context` on carried hook events, `audio_transcript`, and others)
has a verified template that reconstructs the native text from the row's own
fields, used only when the row carries no stored `rendered`/`renderedInHumanTurn`
text; everything else requires that stored rendering and fails without it
("Claude attachment without rendered text has no verified template"). Seven
bundle-only, model-visible kinds (`skill_mention`, `inlined_image_paths`,
`account_memory_recall`, `elapsed_time_reminder`, `proactivity`,
`teammate_mailbox`, `team_context`) stay refused rather than guessed; 2.1.294
renders a larger set of bundle-only kinds to nothing, so those are dropped.

The parent-chain walk prefers a boundary's `logicalParentUuid` when it points to
an entry before the boundary; when it does not (missing, forward, or past the
boundary), it falls back to the last preserved pre-boundary row named by the
boundary's own retained-segment or preserved-message metadata. A trailing tool
call left unresolved when the transcript ends gets Claude's own synthetic
`is_error` result, `"[Tool call interrupted: the session ended before this
call's result was recorded, so its outcome is unknown]"`, with harness
provenance, rather than being dropped; assistant rows that share one
`message.id` merge into a single response. The writer round-trips an
independent post-checkpoint suffix through a `ringleaderFollowingContext`
marker on the boundary row, and a retained-segment checkpoint's preserved
historical copies are checked against the native history they reference
before the writer restores the canonical `logicalParentUuid` reference.

## Pi

Self-verified writer: pinned known-good versions `0.85.1` and `0.87.1`; any other installed
version (1.0.1, measured here) self-verifies first — see Self-verification above. The v3 session
format is required regardless of version. References are the originally pinned package
`@earendil-works/pi-coding-agent@0.85.1` (the design's source of record for the format;
behavior at newer self-verified versions is proven by the harness itself, not re-read from
source), `docs/session-format.md`, `dist/core/session-manager.js`, `dist/core/messages.js`, and
`dist/cli/args.js`.

The reader requires a v3 header, follows the selected entry's `parentId` chain,
and defaults to the last persisted entry. Extension state entries do not enter
context; `custom_message` and branch summaries do. A custom message with
`display=false` stays context-only. The canonical model cannot currently combine
that visibility split with a checkpoint, so that case fails explicitly. Both
`firstKeptEntryId` and documented embedded `retainedTail` checkpoints are readable.

`role: "system"` message entries are dropped unconditionally: Pi recomputes its
leading system message fresh every turn from the live environment
(`buildSystemPromptState`/`normalizeContext`), so a persisted system entry is
harness bootstrap the destination regenerates, never load-bearing conversation
state. A compaction's own `systemMessage` field (the replayed prompt snapshot
`appendCompaction` records for display) is dropped the same way. `usage` entries
carry model-attributed token accounting that never enters context and are
ignored. A `context_edit {targetId, replacement}` entry replaces or removes its
target's model-context content exactly as the installed runtime's
`buildSessionProjection`/`projectContextEntry` do: a `null` replacement drops the
target from `context` only, and a non-null one substitutes its content, while
`history` keeps the original entry unedited.

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
`errorMessage` payloads, assistant images, pending assistant states, missing or
unknown assistant stop reasons, interrupted pending tool exchanges, unknown
roles/blocks, non-image attachments, and visibility splits that would promote
hidden context into visible history.

Pi tool results must follow their calling assistant contiguously. Prose within
that assistant and several contiguous results are supported; `_validate_replay`
enforces contiguity on every message actually read or staged, but reading resolves
most apparent violations before that check runs at all. Any Pi tool call still
unanswered when a later user/assistant/`custom_message`/`branch_summary`/
`compaction` entry arrives, or when the file ends, gets `transformMessages`'s own
synthetic `ToolResult(is_error=True, output=[Text("No result provided")])` with
harness provenance, matching the installed runtime's own orphan-call synthesis —
this fires on every one of those later-entry kinds, not only ahead of a dropped
aborted turn. A call answered instead by a later result in the file has nothing to
pair with once that earlier context is resolved, so that shape still fails
explicitly ("Pi tool call was interrupted and answered later in the session;
cannot import safely") rather than importing a contradictory transcript.

An assistant with `stopReason` `aborted` or `error` is itself dropped from both
`history` and `context`, matching `transformMessages`: the turn cannot be
replayed, but the conversation around it can still be imported, and its own
unanswered tool calls are closed the same pending-call way.

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
Beyond the structural, declared normalizations each writer lists (JSON
tool-argument whitespace, model-view-history, call-ordered-results,
joined-tool-output, adjacent-text), annotations apply no other content
substitution; regenerated message IDs/parent references do not change ordered
message semantics.

## Repeatable native verification

The Claude and Pi harnesses ship inside `rl` (`ringleader/portability/verify/`);
`rl doctor --verify-conversion [AGENT]` runs them and records each pass. The script
wraps them, and optionally runs the dev-only `claude-agent-sdk` picker probe when
`--claude-sdk` names a disposable SDK install. Pi's session module is resolved from
the installed package beside the `pi` executable:

```sh
validation_root="$(mktemp -d "${TMPDIR:-/tmp}/rl-native.XXXXXX")"
uv pip install --target "$validation_root/sdk" claude-agent-sdk==0.2.156
uv run python scripts/smoke-portability-native.py \
  --claude-sdk "$validation_root/sdk" \
  --evidence "$validation_root/evidence"
```

Optional `--claude` and `--pi` arguments select executable paths. The actual installed
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

Bare mode is not production resume for `attachment` rows. In 2.1.294,
`--bare`/`CLAUDE_CODE_SIMPLE` skips every attachment except `queued_command` and
`poll_events` while building the request (`r_n`: `if(!yr())return!1;...return
n!=="queued_command"&&n!=="poll_events"`). A normal `claude --resume` sends them as
`isMeta` user text, using the stored `rendered` when present and the kind's
template otherwise. The harness's attachment case therefore drops `--bare` and
`CLAUDE_CODE_SIMPLE` and keeps everything else above. Measured 2026-10-08: native
and converted requests match for a stored-`rendered` `nested_memory` and a
template-only `edited_text_file` after a preserved compaction. Under bare flags,
native omits both.

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

Self-verified writer: pinned known-good versions `0.155.1` and `0.157.0`; any other installed
version (0.161.0, measured here) self-verifies first — see Self-verification above, including
the gated `state_5.sqlite` schema identity recorded alongside the version. The reader recognizes
legacy JSONL independently of the installed executable. References are pinned to [Codex rust-v0.155.1](https://github.com/openai/codex/tree/rust-v0.155.1/codex-rs):
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
ordinary user content. `SessionMeta.base_instructions` and a nonempty assistant
message `phase` are each wrapped as a Codex-only `Opaque` (`codex.base_instructions`,
`codex.phase`); `base_instructions` carries harness provenance and is hidden, so the
target's own current policy and project instructions still load unset by it.
`portable()` drops both kinds for every non-Codex target rather than asking a writer
to represent them — a Codex→Codex conversion is the only one that keeps either. The
reader also treats `turn_context`, `world_state`, `inter_agent_communication_metadata`,
`token_usage_record`, `security_risk_score`, and `realtime_item` rows (and
`token_count`/`thread_settings_applied`/`thread_goal_updated` events) as inert runtime
bookkeeping and ignores them, in both the legacy and paginated readers.

The reader is split across `codex.py` (legacy/shared grouping, checkpoints, the
writer), `codex_items.py` (content/attachment/metadata decoding shared by both
legacy and paginated rows), and `codex_paginated.py` (the `history_mode: paginated`
reader, compaction decoding, thread-rollback handling) — the split exists only to
avoid a circular import between the legacy and paginated paths. `session_meta.
history_mode` of `legacy` or `paginated` are both read directly; an inherited
(forked) history (`history_base` set, or a subagent start ordinal) still requires a
reader profile this adapter does not implement.

Content classification walks each model-context block's kind in
`internal_chat_message_metadata_passthrough.content_item_kinds` when present: a
fixed catalog of harness-bootstrap kinds (AGENTS.md/project/environment/skill/plugin
instructions, token-budget and current-time bookkeeping, and the like) is dropped
structurally, a smaller carried-kind catalog (skill instructions, subagent
notifications, hook context, aborted-turn notices, user shell commands,
inter-agent messages) is kept as hidden harness text, and anything else is kept as
authored conversation. Without that marker — older rollouts, or content ringleader
wrote itself — classification falls back to matching the row's literal
bootstrap/carried wrapper tags. Tool names carry their namespace as `namespace.name`
(for example `mcp__files__read`) directly on the model `function_call`/
`custom_tool_call` item; only the legacy display API's `FunctionCallOutput` item
still refuses a namespace, since it has no model call to pair the name against.

Compactions with separate visible/model suffixes use optional canonical
`Checkpoint.following_context`. Its content must match the active context after
summary and retained context. Older serialized checkpoints default this field to
`None`; all other unknown/missing fields retain strict decoding. Compact conversion
selects visible post-boundary messages by their historical position, preserving
prose even when model IDs/content/provenance differ. A writer that cannot express a
foreign checkpoint shape — including Codex itself, for a checkpoint it did not
produce — writes the checkpoint's model view as an ordinary stream instead of
refusing, rather than rejecting it. The stage records that as a `flattened-checkpoint`
normalization, so the journal and `ConversionResult.normalizations` say which conversions
flattened; a read-back that lost its checkpoints without that record is refused.

Codex's own encrypted `compaction` item is the only native checkpoint this profile
can carry verbatim, and only Codex→Codex: the opaque `encrypted_content` payload
round-trips unchanged, confirmed by a harness capture of Codex 0.161.0 resuming a
ringleader-written rollout and replaying the item exactly, independent of whether
`resume_metadata` is present. Every other target cannot decrypt that payload, so
`portable()` substitutes it with the checkpoint's own collapsed history instead:
retained messages are deduplicated against that collapsed history by matching each
one to its latest equal earlier message walking backward from the end, each collapsed
message claimed at most once and never past a retained message's own ID-matched copy
(mirroring how
rust-v0.161.0 itself builds `retained` newest-first, so only the oldest kept message
can be a truncated duplicate), and if the result still exceeds the 150,000-token
portable budget, whole tool-exchange/turn units of the collapsed history are dropped
oldest-first behind one harness-provenance omission marker naming the count, until it
fits. A refusal (`"converted context too large"`) is reserved for when the
checkpoint's retained context plus later turns alone already exceed the budget.

Every writer that flattens a foreign checkpoint keeps the flattened history's hidden
messages in that target's own native hidden form — except Codex, which declares its
`model-view-history` normalization with `hides=False` and writes them as ordinary
model items instead: Codex keeps distinct visible/model streams even with no
checkpoint at all, and an earlier `hides=True` attempt broke a native paginated round
trip. Codex declares that normalization only for a stage that flattened; otherwise its
visible-history read-back is compared exactly.

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

Codex serializes model tool output bodies without a success boolean. A result written
by this adapter therefore starts with an explicit native text block,
`[Historical tool result: succeeded]` or `[Historical tool result: failed]`, followed
by the untouched original output blocks, and readback verifies that text before
recovering the canonical error flag. A genuine native Codex rollout this adapter did
not write carries no such marker: rust-v0.161.0 never serializes a success boolean
into what the model sees, so the reader instead infers the error flag from a visible
item sharing the call ID (its own recorded success/failure), or, when no visible item
matches, from whether the model output's own text begins with Codex's `Script
failed\nWall time ` header — the only failure signal a headerless handler error ever
reaches the model with. Native loopback request captures prove both writer-side
markers survive, including success/failure results with identical original output.
Annotations never substitute for missing native text, tool data or images.

Unsupported shapes fail explicitly: an inherited (forked) history, or one needing
another root session identity; a thread rollback that follows a compaction (a
rollback before one is read, but the model's post-cut reordering of
retained/accepted messages is not reproduced); unknown required rows/events/response
blocks outside the tolerated runtime-record set above; incomplete tasks/tool pairs;
non-image/audio/remote/local-path attachments; non-default image-detail hints;
assistant images; MCP structured content. Structurally tagged hidden reasoning is
excluded. Visible tool families beyond MCP, complete exec, patch and matched
completed function outputs fail explicitly. Empty canonical messages and visible
images without preceding user text are also unsupported.

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

The evidence directory must lie outside any repository checkout: Codex loads every
`AGENTS.md` between the git root and its cwd, which breaks the destination-instruction
assertions.

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

## OpenCode 1.18.30–1.18.33 (structural schema gate; CLI-acceptance self-verified)

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

The writer and reader gate is a **structural** fingerprint, not a hash of the DDL text. It covers
the tables the adapter touches (`project`, `session`, `message`, `part`, `session_message`,
`session_input`, `session_context_epoch`): `PRAGMA table_info` (name, type, notnull, default,
pk), `foreign_key_list`, and `index_list` with each index's `index_info` column names, normalized
and sorted, plus the exact sorted `migration` id list. Other tables (including the
`__drizzle_migrations` table a pre-1.2 database keeps), DDL text, quoting, whitespace and column
order are outside it, because every adapter statement names its columns. A database created
before 1.2 and migrated in place therefore has the same identity as a fresh one. Two fixtures pin
this: `tests/portability/opencode-1.18.30.json` (all 37 fresh CREATE statements and 38
migration IDs) and `tests/portability/opencode-upgraded.json` (the in-place-migrated shape:
appended columns, the extra table, the same migrations). The supported identity, shared by
1.18.30 and 1.18.33, is
`opencode-structure-1:252913917eb4927f5835d43964b7fef730c3c4ae6f5ff9b45a32a2a46ffd8047`.
A column, foreign key, index or migration change in a touched table changes the identity and
needs self-verification, which records the new identity. The adapter never initializes or
migrates a live database, and a dry run reads the real destination database only through a
stable temporary copy. A stage or registration recorded under the superseded whole-schema-hash
identity (`opencode-1.18.30:4e1c…`) still decodes and resumes/recovers: that one legacy value is
accepted wherever stage evidence is checked, and the live database is then checked against the
current structural identity instead of the stale literal, since the database's actual structure
never changed.

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
calls followed by their results, successful outputs with images/PDFs, and completed
error outputs without attachments. Full output strings are never truncated. Source
reasoning and recognized accounting/snapshot/patch telemetry are excluded; unknown
conversational fields fail. Native text marked `synthetic` is preserved as authored
text unless a structural native compaction part identifies its role. Original
timestamps and provenance survive in verified structural annotations, while new
sessions have fresh sortable native IDs, current creation and recency, and zero
fabricated usage.

The reader follows `toModelMessages` and `filterCompacted` at v1.18.33:

- **Error and aborted assistants.** An assistant with `error` is skipped (both streams), except a
  `MessageAbortedError` assistant with any part other than `step-start`/`reasoning`, which is
  kept as written. A failed or unfinished compaction summary refuses, since native would not
  apply that compaction.
- **Incomplete assistants** (no `time.completed`/`finish`) are read as written. A `pending` or
  `running` tool reads as the native synthetic error result `[Tool execution was interrupted]`.
- **Interrupted tools.** An `error` tool whose `metadata.interrupted` is true and whose
  `metadata.output` is a string reaches the model as that output, as a success; otherwise the
  `error` string is the result. `state.raw` (the streamed argument text) is never read and is
  dropped.
- **Tool-state metadata.** `toModelMessages` reads only `interrupted` and `output`. These keys
  drive the TUI or tool execution and are dropped: `count`, `description`, `diagnostics`,
  `diff`, `dir`, `display`, `exists`, `exit`, `filediff`, `filepath`, `matches`, `model`,
  `name`, `outputPath`, `parentSessionId`, `preview`, `sessionId`, `todos`, `truncated`.
  `loaded` reaches the model indirectly: `Instruction.extract` reads it to avoid re-attaching a
  nested instruction file's text on a later read. It is dropped for a cross-agent target, where
  it has no meaning, but carried for an OpenCode→OpenCode round trip (same mechanism as the tool
  timestamp annotation), so a continued session does not duplicate that text. Any other key
  refuses.
- **Provider metadata.** `part.metadata.openrouter.reasoning_details` on tool parts is provider
  reasoning and is dropped; any other tool-part metadata (such as `providerExecuted`) refuses.

Native tool parts store a call and result together. The reader represents a step's
ordered calls as one assistant message followed by one tool message per result, in call
order. The writer takes the run of result messages after an assistant's calls, in any order,
and writes each result into its call's part; it requires either one combined result message or
exactly one message per call, refusing any other mix, and records which of the two shapes was
native in the structural annotations, so a single combined result message reads back as one.
Three
normalizations are declared: results replay in call order (`call-ordered-results`); a tool
output's text blocks are joined by a blank line with media after them
(`joined-tool-output`), exactly as native MCP tool results are stored; and JSON arguments are
compared semantically. Message creation times must not decrease natively, so a decreasing
source time is raised to the previous message's time; the original stays in the annotation, and
readback accepts it only where the native time equals that floor. Session metadata carries only
the original provenance, timestamps, historical instruction/omission labels and structural
message boundaries. It does not contain a canonical replay copy that could conceal missing
native output. Mutating a labeled historical instruction or an annotated native timestamp
fails readback. The writer compares full canonical history and active context before
returning a stage.

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
rows; ongoing compaction; ignored text; empty conversational text; tool
`time.compacted`; unclassified tool-state or tool-part metadata; text-part provider
metadata; user `system` instructions; structured output; subtask/agent/retry parts;
prose after tools within one native message; assistant file parts; text-file/directory
transformations; remote attachments; and non-embedded tool attachments. One
real-session shape also stays refused: a duplicate `callID` inside one message (a stray
`unknown` tool part that OpenCode recreates from a late argument delta), which no target
can replay. Attachment placement in actual model requests is provider dependent: the
pinned source retains media in tool results for Anthropic/OpenAI, while other providers
may extract a synthetic user media message. That runtime behavior has not been measured
here.

Project IDs follow native resolution: normalized origin remote hashed with SHA-1,
then the existing common-directory `opencode` cache, then the lexically first root
commit, then `global`. Non-git projects use the filesystem root as worktree.
Like native `fs.up`, the walk for `.git` starts at the destination cwd whether or not it
still exists, but a missing cwd only resolves to `global`: if the walk finds an existing `.git`
ancestor, the destination must exist, so a deleted nested repo is never silently registered
under an unrelated ancestor's project. `rl resume` separately refuses to launch into a missing
directory. Registration resolves again and checks the staged identity; it never writes
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
discovery and a project `opencode.json`/`opencode.jsonc` (or `.opencode/` config) that sets
`model`, `agent`, `mode` or `default_agent` are unsupported; a project file with only other
keys (such as `permission`) cannot change the model and is accepted. Home `.opencode`
JSON/JSONC config, any `default_agent` key, a global `agent`/`mode` definition of `build`,
and discovered `agent`/`agents`/`mode`/`modes` directories under native global, home or
project config roots also fail. These can select a different model before the stored
session model is considered. Definitions of other agents cannot: the writer stamps every
message with agent `build`, and on resume the TUI restores the last user message's agent
and model (`if (!args.agent) local.agent.set(msg.agent)` then `local.model.set(msg.model)`),
which `createUserMessage` prefers (`input.model ?? ag.model ?? currentModel`).
`Compatibility.target_configuration` exposes the resolved provider/model;
`TargetRequest.target_configuration` binds that inspection and makes configuration
drift fail during staging. Resolution is scoped to each target's roots/cwd.
Launch is `opencode --session <new-id>` in the destination cwd, with explicit
HOME/XDG roots and `OPENCODE_DB` selecting the staged native database.

`rl doctor --verify-conversion opencode` (CLI-acceptance tier) runs against the installed
executable directly and has passed here against OpenCode 1.18.33, recording schema identity
`opencode-structure-1:252913917eb4927f5835d43964b7fef730c3c4ae6f5ff9b45a32a2a46ffd8047` — the
same identity documented above for 1.18.30 and 1.18.33. That CLI-acceptance pass establishes
native import/export/list acceptance and the schema gate; it does **not** capture OpenCode's own
outbound model request, which needs a source checkout to invoke the pinned context builder
directly (below). Unit tests and schema/source inspection establish the supported
implementation; this deeper harness is provided for an authorized session with OpenCode
**exactly 1.18.30**, Bun, and a clean official v1.18.30 source checkout whose dependencies are
already installed:

```sh
uv run python scripts/smoke-portability-opencode.py \
  --executable /absolute/path/to/opencode \
  --native-source /absolute/path/to/official-opencode-checkout-at-installed-tag \
  --evidence /absolute/new/opencode-evidence
```

The CLI-acceptance part (`ringleader/portability/verify/opencode.py`, also run by
`rl doctor --verify-conversion opencode`) needs no source checkout; `--native-source`
adds the dev-only context-builder check below.

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

## Droid

Self-verified writer: no version is pinned from upstream research; every installed version
(0.228.0, measured here) self-verifies via `rl doctor --verify-conversion droid` before
conversion — see Self-verification above. References are droid 0.228.0's own
`src/services/sessionJsonl.ts` and offline `/v1/messages` captures of `droid exec --fork`; there
is no public source checkout to pin against.

The native format is JSONL: one `session_start` header (schema version 2, with the session's
`owner` — required; `hostId` is not), then `message`/`compaction_state` events in file order. A
session whose header carries `callingSessionId` is a subagent run, not a resumable top-level
session, and is rejected. The latest `compaction_state` row wins; its `anchorMessage.index` is
not trusted as a boundary locator, since real sessions showed it can be stale — the reader
re-derives the boundary from the transcript itself.

Droid keeps one stream, not two: a message's `visibility` is `llm_only` or ordinary, and the
model-context/hidden split is exactly that flag, carried through `Normalization
model-view-history` with `hides=True` unconditionally (droid has no separate visible-only
suffix to preserve, unlike Codex). A `visibility=llm_only` message is harness bootstrap, dropped
structurally by its native message ID, when that ID starts with `context-` (an injected context
block), `__internal__system-reminder__` (droid's `<system-reminder>` injections: nested project
instructions, skill catalogs, repeated-call and ending nags), `todo-stale-` or `todo-suggest-`
(TodoWrite nags). Droid regenerates each of these itself, and nested project instructions reach
another agent through its own instruction loading. Every other `llm_only` message — on this
machine's store, plain-UUID interrupt markers ("Request interrupted by user") and
`task-completion:` background-task notices, both records of what happened in the session — is
carried as hidden user-role text. A tool call left unanswered when a later
message arrives, or answered out of order, fails to import (`"Droid tool call was answered after
an intervening message; cannot import safely"`); a call whose result was dropped entirely reads
back as droid's own synthetic cancellation text (`"Error: Tool execution was cancelled"`), not a
fabricated error.

Two compaction kinds are native-expressible and round-trip exactly: `llm_summary` (droid's own
model-written recap) and `provider_switch_serialization` (its model-provider-compatibility
serialization, confirmed to replay correctly). Each requires its summary message to carry
droid's literal wrapper text — a fixed prefix/suffix the reader strips and the writer
regenerates — plus a contiguous preceding retained tail; writing recovers the kind from the
wrapper text and fails (`CheckpointShape`) if it does not match either one. Any other
checkpoint — a foreign agent's summary, or one that does not carry droid's own wrapper — is
flattened to an ordinary stream instead of refused, the same model-view rule as every other
writer. Opaque content (another agent's untranslatable payload) is rejected outright; it must
already have been substituted by `portable()` before staging.

The writer requires an `owner` resolved from a local top-level session (`local_owner()`, newest
by mtime with a matching header) and fails explicitly when no local session can supply one.
Session, message, and tool-result identity follow the same embedded-image and
JSON-argument-whitespace normalizations as the other adapters. Launch is `droid resume <id>`;
storage is keyed by `slug.droid_slug(cwd)`, droid's own directory encoding (see
[AGENTS.md](../../AGENTS.md) and [agent-session-management.md](agent-session-management.md)),
not Claude Code's.

### Repeatable Droid verification

```sh
uv run rl doctor --verify-conversion droid --verified /absolute/new/verified-targets.json
```

The harness stages a representative conversation (plain, a failed tool result, a cancelled
unanswered call, each compaction kind, and a foreign/model-view summary) into a disposable
`~/.factory`-shaped store with a seeded owner session, then runs `droid exec --fork <id> -m
custom:<mock-model> OFFLINE_CAPTURE_ONLY` against a loopback HTTP mock that returns HTTP 400
before any inference. It asserts the single captured `/v1/messages` request: role order, the
historical instruction and image content, tool call/result pairing and error flags, the
cancellation text, hidden `llm_only` context replayed in place, and the native summary wrapper
for each compaction kind, then checks `droid exec` never starts Factory's login flow and that
the staged file bytes are untouched. No external inference and no real tool execution occur.

## Limits

Every snapshot — a single transcript file, and for Codex and OpenCode the entire native state
database — is bounded by the same `native.MAX_BYTES` cap, 128 MB. For a single transcript this
is generous; for the two agents with a shared database it is not incidental. Codex's
`state_5.sqlite` (45 MB measured here) and OpenCode's `opencode.db` (46 MB measured here) are
each read in full as one snapshot, because both the name index and the session graph live in
that one file alongside every other session. Once either database crosses 128 MB, every
conversion *from* that agent refuses until it shrinks, independent of how small the individual
session being converted is.

Conversion journals under `conversion/` are never pruned. Each operation keeps its own copy of
the source snapshot, the staged target bytes, and the publication intents, so one converted
session accumulates three to four full copies of its content on disk, indefinitely. This is a
known gap, not an oversight: nothing currently calls for it to be bounded, and recovery depends
on the journal being present until a human or `rl data transaction` acts on it.

## Final pairwise conversion sweep

Measured 2026-10-08 against the self-verified versions above: Claude Code 2.1.294, Codex
0.161.0, Pi 1.0.1, OpenCode 1.18.33, Droid 0.228.0. `scripts/conversion-sweep.py --source
<agent> --all-targets` dry-run-converts every eligible real local session (most recent 200 each
for Claude Code and Codex, every available session for Pi, OpenCode, and Droid) against every
target and tallies normalized refusal reasons. Sessions with nothing to convert are excluded and
counted separately, never as refusals.

| source \ target | claude-code | codex | opencode | pi | droid |
|---|---|---|---|---|---|
| claude-code | 197/200 | 197/200 | 197/200 | 197/200 | 197/200 |
| codex | 160/164 | 162/164 | 160/164 | 160/164 | 160/164 |
| opencode | 164/166 | 164/166 | 164/166 | 164/166 | 164/166 |
| pi | 39/39 | 39/39 | 38/39 | 39/39 | 39/39 |
| droid | 40/40 | 40/40 | 40/40 | 40/40 | 40/40 |

Excluded, inherently empty (no conversation to convert, same count for every target): 36/200
Codex rollouts with no conversation turns; 3/43 Droid sessions with no conversation messages.

Remaining refusal categories, normalized to categories only:

- **Claude Code source, every target** (1 session each): a source transcript over the snapshot
  byte limit; a `file`-kind attachment with no verified template and no stored rendering; a
  compact boundary missing its summary.
- **Codex source, every target** (1 session each): a source rollout over the snapshot byte
  limit; an inherited (forked) history needing a reader profile this adapter does not implement.
- **Codex source, non-Codex targets only** (2 sessions): the converted context exceeds the
  target's token budget even after oldest-first trimming — Codex→Codex avoids this because it
  keeps the encrypted compaction payload natively instead of substituting and trimming it.
- **OpenCode source, every target** (2 sessions): a duplicate tool-call part under one call ID
  that OpenCode itself re-created from a late argument delta; no target can replay two parts
  under one id.
- **Pi source → OpenCode only** (1 session): a Pi message whose text OpenCode's native context
  builder omits as empty, which this writer does not yet reconstruct.

Every category sits at or under roughly 1.2% of its pair's eligible sessions, except the single
Pi→OpenCode session (2.6% of 39) — the bar this run worked to tolerates any one-session category
regardless of its percentage, and stops only at a category that is both above 2% and more than
one session. None of the five sources hit that.
