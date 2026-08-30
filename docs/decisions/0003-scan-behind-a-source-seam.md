# 0003: Put scanning behind a `Source` seam

## Context

The first implementation read `~/.claude/projects/*.jsonl` directly, with Claude Code's
transcript shape assumed throughout the registry and view code. Supporting a second agent would
have meant threading a format switch through every layer.

Agent session stores differ in ways that go past file format: some keep a **separate index**
of sessions (Codex's sqlite `threads` table) that a file deletion leaves stale, some key
storage by the directory a session launched in, and some support renaming natively while others
do not. A format-only abstraction would not have covered any of that.

## Decision

`ringleader/sources/` defines a `Source` protocol with two required methods — `discover()`
(cheap: paths and stat metadata, no parse) and `parse()` (read one into a `NormalizedSession`).
Everything above it — registry, overview, triage — talks to `Source`, never to a transcript
format.

Capabilities beyond the core are **separate opt-in protocols**, each advertised in a source's
`capabilities` set and checked with `isinstance` at the call site:

| Protocol | For an agent that |
|---|---|
| `NameApplyingSource` | supports native renaming |
| `PickerHidingSource` / `PickerShowingSource` | keeps its own session index that a file move would strand |
| `WorkspaceRelocatingSource` | keys session storage by the launch directory |
| `CachedMetadataSource` | has metadata worth refreshing even on a cache hit |

## Consequences

- Adding an agent means writing one module, not touching the registry.
- Callers must ask for a capability rather than assume it; a source that lacks one degrades to
  a no-op instead of an error.
- `NormalizedSession` carries a few Claude-Code-shaped extras (`message_count`, `git_branch`,
  `ai_title`) because they drive real behavior — prune eligibility and display. A source that
  cannot supply them leaves the defaults.

See [tech/sources.md](../tech/sources.md).
