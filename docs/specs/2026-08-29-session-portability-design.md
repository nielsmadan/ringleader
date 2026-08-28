# Session Portability and Shared Presence — Design

**Date:** 2026-08-29
**Status:** Approved design, pre-implementation

## Goal

Make sessions portable across Claude Code, Codex, OpenCode, and Pi: fork or convert a
conversation into another agent's native resume store, resume it through one generic command,
retarget sessions to another directory, and move a whole workspace while preserving the relative
cwd of every session beneath it.

The design also extracts Juggler's agent hooks into **Hooklinesinker**, a small standalone presence
CLI shared by Juggler and Ringleader. This gives `rl` a dependable `--running` selector without
requiring Juggler to be installed or running.

## Delivery boundaries

This is one user-facing workflow but three implementation projects. Each gets its own plan and
lands independently:

1. **Hooklinesinker:** shared hook installation, normalized status, durable presence, and Juggler
   migration.
2. **Session portability:** canonical conversation model, four agent adapters, conversion,
   compact conversion, forking, resuming, handoff, and lineage.
3. **Relocation:** native session retargeting, workspace moves, transaction recovery, and bulk
   path repair.

The projects share protocols, not implementation state. Hooklinesinker is needed only for
`--running` selection and move safety; conversion by explicit ID and `--open` work without it.

## Settled vocabulary

- **Open:** unresolved work according to Ringleader's lifecycle registry. This is a work-state
  classification, not a process signal.
- **Running:** a session attached to a verified live agent process. Hooklinesinker supplies this.
- **Fork:** create one independent session from the source session's current tip.
- **Convert:** the batch-oriented cross-agent form of a fork. The source is retained.
- **Move a session:** change its native resume cwd without moving workspace files.
- **Move a workspace:** move the real directory and retarget all sessions beneath it.
- **Retarget a workspace:** repair all session paths after the directory was moved externally.

The CLI does not use “in-flight.” It does not merge divergent sessions.

## Architecture

```text
agent hooks/plugins
        │
        ▼
Hooklinesinker ── durable status ──► rl --running
        │
        └── registered realtime sink ──► Juggler

native session ── agent reader ──► canonical conversation ── agent writer ──► native session
                                             │
                                             └── compact transform
```

Ringleader's existing `Source` seam remains the cheap discovery and summary path used by scans.
Deep conversation parsing and native writes are separate adapter capabilities so a routine scan
never pays the cost or assumes the risk of full transcript conversion.

An agent integration can provide these independent capabilities:

- `ConversationReader`
- `ConversationWriter`
- `SessionLauncher`
- `SessionRelocator`
- the existing picker/name capabilities

Commands gate on declared capabilities. They do not switch on the agent name in command code.

## Hooklinesinker

### Responsibility

Hooklinesinker is low-level plumbing, not a daemon, SDK, event bus, or application framework. It:

1. Installs and removes the supported agents' hooks, plugin, and extension.
2. Accepts native events on stdin.
3. Normalizes the requested fields into a versioned status event.
4. Atomically maintains a local presence ledger.
5. Forwards the event to registered realtime sinks.
6. Reports installation, consumer, hook, sink, and status health.

Every hook invocation is short-lived. A failed write or sink is recorded for diagnostics, but the
hook exits successfully so it cannot break the agent.

### Version 1 status capability

The only v1 capability is `status`. Its normalized record contains:

- protocol version and observation time
- agent and native session ID
- cwd and transcript locator, when the agent exposes them
- normalized phase: `idle`, `working`, `permission`, `compacting`, or `unknown`
- process ID, process start time, host identity, and available terminal identity
- the normalized lifecycle event that produced the state

Hooklinesinker finds the owning agent process by walking the hook process's ancestors. Process ID
plus start time prevents PID reuse from reviving an old session. Session-end events clear a binding
promptly; queries also verify the process, which handles crashes and agents without a dependable
end event. If process identity cannot be verified, liveness is `unknown`, never `running`.

Codex does not emit `SessionStart` until the first prompt, so an empty newly opened Codex process
has no discoverable running session yet. Once a session has emitted an event, process verification
is authoritative.

### Future data

Consumer registration declares named capabilities:

```json
{
  "consumer": "juggler",
  "capabilities": ["status"],
  "sink": "http://127.0.0.1:7483/hook"
}
```

Hooklinesinker collects the union required by installed consumers, persists only the fields each
capability defines, and sends each sink only its subscribed capabilities. Later releases may add
capabilities such as activity detail or usage. A sink cannot request arbitrary native payload
storage; new fields require a named, versioned capability. Raw native input is discarded after
extraction and is never logged or forwarded.

### Storage and CLI contract

The installed implementation is private to Hooklinesinker. Consumers use its JSON CLI, not its
files directly.

```text
${XDG_DATA_HOME:-~/.local/share}/hooklinesinker/
├── bin/hooklinesinker -> ../versions/0.2.0/hooklinesinker
├── versions/<version>/hooklinesinker
└── consumers/<consumer>.json

${XDG_STATE_HOME:-~/.local/state}/hooklinesinker/
└── status/...
```

The small public surface is:

```sh
hooklinesinker install --consumer NAME [--sink URL]
hooklinesinker uninstall --consumer NAME
hooklinesinker ingest --agent AGENT --event EVENT
hooklinesinker sessions --json
hooklinesinker consumers --json
hooklinesinker doctor
hooklinesinker version --json
```

Agent hook configuration contains the expanded absolute path to the stable binary. Claude and
Codex hooks pipe native JSON into `ingest`; the OpenCode plugin and Pi extension are thin adapters
that spawn the same command. They contain no consumer-specific endpoint or payload-normalization
logic.

### Packaging and ownership

Hooklinesinker is a separate Rust project producing self-contained macOS and Linux binaries. Rust
avoids requiring Python for Juggler, Swift for Ringleader, or a shell JSON/locking implementation.

Both products pin and vendor a release:

- Juggler embeds a signed universal macOS binary in its app bundle.
- Ringleader's platform distribution includes the matching platform binary.
- Juggler's remote installer downloads its pinned release artifact and verifies its checksum.

The user does not install a Homebrew dependency. Brew may be offered later for standalone use.

The bundled binary installs itself into the neutral, versioned location. Installers activate the
newest protocol-compatible version atomically and never downgrade. A newer incompatible version
is left untouched and consumer registration fails with an upgrade instruction. Hooklinesinker's
protocol compatibility, not exact package-version equality, decides whether a consumer can reuse
the installation.

Hooks belong to the registered consumer set rather than to Juggler or Ringleader. Installing the
second product reuses the existing binary and hooks, then adds its consumer record. Uninstalling a
consumer removes its sink; the hooks and active binary are removed only after the last consumer is
gone. Migration removes Juggler's old direct hook groups before installing Hooklinesinker groups,
preventing duplicate events.

Juggler hydrates its initial state from `hooklinesinker sessions --json` and receives later changes
through its registered HTTP sink. Ringleader invokes the same JSON query when it needs running
sessions and registers no sink.

## Canonical conversation model

Conversion uses one versioned intermediate representation rather than twelve pairwise converters.
It records ordered, visible conversational context:

- user, assistant, system, and user-supplied instruction messages
- structured content parts and attachments when portable
- tool calls and their matching results
- compaction boundaries and summaries
- source branch tip and cross-agent lineage
- source agent, native ID, timestamps, cwd, and format version

It excludes UI state, caches, token accounting, and hidden or encrypted model reasoning. Recognized
agent bootstrap prompts are not copied as instructions for the target agent. The target reloads
the current project instructions from the destination cwd; user-authored instructions in the
conversation remain part of the history.

Pi and any future tree-shaped source are flattened to the currently selected tip and its ancestors.
The source branch identity is recorded, but v1 does not reproduce the whole tree or fork from an
arbitrary historical turn.

The representation is internally serializable from the start. A portable `.rlsession` bundle and
cross-machine import/export are follow-up features; the existing `rl data export` remains the
human-readable Markdown export.

## Conversion and validation

A conversion is out-of-place and transactional:

1. Resolve and version-check the source and target adapters.
2. Parse the source into the canonical model.
3. Apply the optional compact transform.
4. Report any unsupported context before writing.
5. Write a new native target session into staging.
6. Parse the staged target back into the canonical model.
7. Compare meaningful content and validate the target's picker metadata.
8. Atomically publish the session to the native store and record lineage.

Unknown telemetry is ignored. Unknown conversational content fails by default rather than silently
disappearing. Tool history is historical and is never re-executed. Native format versions are
explicitly supported; an unknown writer format fails before touching the target store.

Each target receives a new native session ID. Cross-agent lineage is authoritative in Ringleader's
registry and is also written into native lineage fields when the target format supports them.
Batch conversion commits per session: one malformed source does not discard successful conversions,
the command exits nonzero if any failed, and rerunning does not duplicate completed targets.

Same-agent forks prefer an official native operation when it can satisfy the command's behavior.
Otherwise they use the same reader/writer round trip as cross-agent conversion.

### Compact conversion

`--compact` is deterministic and makes no model call. It:

- preserves instructions and user/assistant prose
- keeps the latest compaction summary while removing the history it supersedes
- replaces contiguous tool exchanges with a short marker containing tool names and counts
- preserves later compaction summaries
- adds a short notice that tool activity was omitted during import

Tool calls and results are removed together so the target never receives dangling tool-call IDs.
The flag authorizes exactly this loss; other unsupported conversational content still fails.
Ringleader reports approximate full and compact token sizes plus omitted segment and tool counts.
A future model-generated summary is a separate `--summarize` feature.

## Ringleader command surface

```sh
rl session convert <id>... --to AGENT [--compact] [--dry-run]
rl session convert --open --from AGENT [--workspace PATH] --to AGENT [--compact] [--dry-run]
rl session convert --running --from AGENT [--workspace PATH] --to AGENT [--compact] [--dry-run]

rl session fork <id> [--to AGENT] [--compact] [--resume]
rl session handoff <id> --to AGENT [--compact]
rl session resume <id>
rl session lineage <id>

rl session move <id>... --to PATH [--dry-run]
rl workspace move OLD NEW [--dry-run] [--yes]
rl workspace retarget OLD NEW [--dry-run] [--yes]

rl data transaction list
rl data transaction resume <id>
rl data transaction rollback <id>
```

`convert` creates target sessions without launching them. `fork` is the one-session primitive;
`--resume` enters the result. `handoff` is the explicit convenience form of `fork --to ... --resume`.
`resume` resolves the native ID and cwd, then replaces itself with the correct agent command.

Explicit IDs may be unique prefixes. Ambiguous prefixes require an agent-qualified identifier.
Batch selectors are mutually exclusive with explicit IDs. `--open` uses Ringleader's lifecycle
classification; `--running` queries Hooklinesinker and fails with installation guidance when
status support is absent. Agent names are `claude`, `codex`, `opencode`, and `pi` at the CLI.

Conversions and forks retain their source. Archiving it is always a separate explicit operation,
which keeps outage recovery reversible.

The current `rl session relocate` only pins registry metadata. It is replaced by `session move`,
which updates both native resume state and the registry. This pre-1.0 CLI follows the repository's
existing hard-cut policy rather than retaining a misleading alias.

`rl doctor` is extended to cover Hooklinesinker, adapter compatibility, native picker health, and
unfinished transactions. There is no second session-specific doctor command.

## Session and workspace moves

`session move` changes the native cwd and any native storage location derived from it, but never
moves workspace files. It rewrites structural path fields only. Historic prompts, commands, tool
arguments, and tool results are not search-and-replaced.

`workspace move OLD NEW` moves the real directory and maps every known session at or beneath `OLD`
to the corresponding relative cwd beneath `NEW`. It includes open, dormant, kept, and archived
sessions across all adapters. `workspace retarget` performs the same session mapping when the
filesystem move already happened elsewhere.

Moves refuse to proceed while any affected session is running. An external process cannot safely
change a live agent's cwd and open-file assumptions, so v1 has no force override.

Every move uses a durable transaction under Ringleader's data directory:

1. Refresh discovery and resolve every affected native record.
2. Verify liveness, destination collisions, adapter versions, permissions, and disk space.
3. Detect linked Git worktrees and plan official `git worktree move` operations.
4. Prepare and round-trip-validate rewritten session state in staging.
5. Show the complete plan and require confirmation unless `--yes` was supplied.
6. Move the workspace and publish native session rewrites.
7. Update Ringleader's registry and commit the journal.

Same-filesystem directory renames are preferred. A cross-filesystem move copies into destination
staging, verifies it, retains the source until session publication succeeds, and deletes the source
only at commit. Unsupported nested linked-worktree layouts fail during preflight.

Backups remain available until commit. A caught failure rolls back automatically. A crash leaves a
journal that `rl doctor` surfaces and `rl data transaction resume|rollback` can recover. Repeating
an operation resumes its transaction rather than creating a second conversion or move.

## Error policy

Conversion and move commands fail before publication for:

- unknown source or target transcript versions
- absent target agents whose native format cannot be established
- unsupported conversational content not covered by the defined compact transform
- session-ID, picker-index, or filesystem collisions
- running sessions affected by a move
- unsafe Git-worktree layouts
- insufficient staging space
- failed round-trip validation

A conversion failure leaves its source untouched and its target quarantined outside the picker. A
workspace failure either rolls back or leaves an explicit recovery transaction. No operation
silently drops context, replaces a source session, rewrites arbitrary historic text, or weakens a
verification step to make a format appear supported.

## Testing

Tests never use the real agent stores. Every adapter takes explicit roots and runs against isolated
temporary HOME/XDG directories.

- Keep sanitized native fixtures for every supported agent format version.
- Snapshot each reader's canonical output.
- Write every canonical fixture through every target writer, read it back, and compare meaningful
  context.
- Repeat the matrix with `--compact`, asserting exact omission markers and no dangling tool calls.
- Verify target visibility and resume command construction against isolated native indexes.
- Run actual-agent smoke checks without provider calls when the installed CLI permits it.
- Fault-inject every transaction phase and verify cleanup, rollback, resume, and idempotency.
- Cover ordinary directories, same-filesystem moves, cross-filesystem staging, normal Git repos,
  linked worktrees, archived sessions, and mixed-agent workspace trees.
- Exercise Hooklinesinker event normalization, concurrent writers, missing end events, process
  crashes, PID reuse, sink failure, status hydration, and capability filtering.
- Exercise Juggler-first and Ringleader-first installation, compatible upgrades in both orders,
  incompatible-version refusal, and uninstalling either consumer while the other remains.

Adding support for a new native format version requires a fixture and permanent regression test.

## Deferred and excluded

- Merging conversations
- Reproducing a full source branch tree or choosing an arbitrary historical fork point
- Model-generated conversion summaries
- Arbitrary sink-defined payload capture
- A long-running Hooklinesinker daemon
- Requiring Homebrew for normal installation
- Cross-machine sync and portable `.rlsession` import/export UI
- Windows packaging in the first Hooklinesinker release
