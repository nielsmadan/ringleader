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
