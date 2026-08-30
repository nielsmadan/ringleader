# Sources

`ringleader/sources/`: the seam between ringleader and each agent's session store.

## Purpose

Every agent stores sessions differently, and the differences go past file format — some keep a
separate index that a file move would strand, some key storage by launch directory, some
support renaming natively. `sources/base.py` defines a small required protocol plus a set of
**opt-in capability protocols** for the rest, so the registry and views never know which agent
a session came from ([0003](../decisions/0003-scan-behind-a-source-seam.md)).

Implementations today: `claude_code.py` (`~/.claude/projects/*.jsonl`) and `codex.py`
(`~/.codex/sessions/**/*.jsonl`, with native names from `~/.codex/session_index.jsonl`).

## How it works

### The two data carriers

- **`RawRef`** — what `discover()` yields: `source`, `file`, `mtime`, `size_bytes`, and an
  optional `fingerprint`. Deliberately stat-only, so enumerating a large store parses nothing.
- **`NormalizedSession`** — what `parse()` returns. A cross-harness core (`session_id`, `cwd`,
  `custom_title`, `summary`, `last_prompt`, …) plus Claude-Code-observed extras
  (`message_count`, `git_branch`, `ai_title`) that earn their place by driving real behavior:
  `message_count` gates prune eligibility, the others are display and classification context. A
  source that cannot supply them leaves the defaults.

`tail_text` is transient — used for classification, never persisted.

### The required protocol

```
class Source(Protocol):
    name: str
    capabilities: frozenset[str]
    def discover(self) -> Iterator[RawRef]: ...
    def parse(self, ref: RawRef) -> NormalizedSession: ...
```

The two-phase split is what makes incremental scans cheap: `discover()` is stat-only, and
`parse()` runs only for refs the registry's fingerprint check rejects.

### Capability protocols

Each is `@runtime_checkable` and gated by a string in `capabilities`. Callers check **both** —
the set says the source opts in, the `isinstance` satisfies the type checker.

| Protocol | Capability | Methods |
|---|---|---|
| `CachedMetadataSource` | — | `refresh_cached_metadata()` — update display fields even on a cache hit |
| `NameApplyingSource` | `applies_names` | `apply_name()` — write a name into the agent's own store |
| `PickerHidingSource` | `hides_from_picker` | `hide_from_picker()` |
| `PickerShowingSource` | `shows_in_picker` | `show_in_picker()` |
| `WorkspaceRelocatingSource` | `relocates_workspace` | `storage_for()`, `relocate_storage()`, `relocate_session()` |
| `WorkspaceRebindingSource` | `rebinds_workspace` | `rebind_workspace()` |

**Picker hiding** exists because Codex keeps a sqlite index separate from the rollout files.
Deleting or moving a transcript there leaves the picker offering a session whose file is gone.
`scanner.hide_from_picker()` clears the entry first and **raises** if the source has an index it
cannot update — callers must not proceed to move the file in that case.

`show_in_picker()` is the inverse, and takes the transcript's **original** path, because the
index still points there. The file has to be back in place before it runs.

**Workspace relocation** exists because Claude Code keys storage by the directory a session was
launched in. Both relocate methods **raise rather than merge** when the destination is occupied;
the caller checks up front and refuses, so a move never half-completes.

**Workspace rebinding** is the other shape the same problem takes. Codex files rollouts by date
and records the workspace in a `cwd` column of its state db, which `codex resume` filters the
picker on — so a move relocates nothing and rewrites that one entry instead. Nothing can collide
and no transcript path changes, which is why it is a separate protocol rather than a branch
inside the relocating one. Miss it and the session stays reachable only by its id.

All three index writers (`hide_from_picker`, `show_in_picker`, `rebind_workspace`) return an
**`IndexUpdate`**, not a bool: `NO_INDEX` (this source keeps no index here) and `NO_ENTRY` (it
does, and the session is not in it) need opposite handling from the caller, and
`ALREADY_CURRENT` is success on a re-run rather than a failure to report.

### Helpers

- `stat_fingerprint(st)` — `dev:inode:size:mtime_ns:ctime_ns`, the registry's cache key.
- `append_jsonl_line(path, line)` — appends a record, inserting a leading newline only when the
  file does not already end with one. Writing a name into a live transcript must not corrupt it.

## Key entry points

- `sources/base.py` — every protocol and both dataclasses.
- `scanner.SOURCE_TYPES` — the registered implementations.
- `scanner.active_sources()` — those whose root actually exists.
- `scanner.source_by_name()` — resolve a thread's `source` string back to an instance.

## Gotchas

- **`scanner.SOURCES` is built at import time; `active_sources()` builds fresh instances.**
  The module-level tuple would otherwise capture whatever root was current at import, which
  breaks under monkeypatched environment variables in tests.
- **A missing capability is a no-op, not an error.** `hide_from_picker()` returns `False` for a
  source without an index. Only a source that *has* one and fails raises.
- **`parser_version` is the cache-invalidation lever.** Change how a source parses and bump it,
  or every existing thread keeps its stale cached parse.
- **`git_resolved` marks whether git identity came from this scan** rather than the cache —
  the registry needs it to decide whether to overwrite the git fields.

## Why

Capability protocols rather than one fat interface with optional methods: a source declares only
what it can do, the type checker verifies the call sites, and adding an agent whose store has a
quirk nobody anticipated means adding a protocol rather than widening every implementation.

## Related

- [0003: the Source seam](../decisions/0003-scan-behind-a-source-seam.md)
- [reference/agent-session-management.md](../reference/agent-session-management.md) — the
  external behavior these protocols model, verified per agent and version
- [registry.md](registry.md) — how `NormalizedSession` lands in state
