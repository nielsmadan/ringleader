# Sources

`ringleader/sources/`: the seam between ringleader and each agent's session store.

## Purpose

Every agent stores sessions differently, and the differences go past file format — some keep a
separate index that a file move would strand, some key storage by launch directory, some
support renaming natively. `sources/base.py` defines a small required protocol plus a set of
**opt-in capability protocols** for the rest, so the registry and views never know which agent
a session came from ([0003](../decisions/0003-scan-behind-a-source-seam.md)).

Implementations today: `claude_code.py` (`~/.claude/projects/*.jsonl`), `codex.py`
(`~/.codex/sessions/**/*.jsonl`, with native names from `~/.codex/session_index.jsonl`),
`droid.py` (`~/.factory/sessions/<enc-cwd>/*.jsonl` plus `.settings.json` sidecars), `pi.py`
(`~/.pi/agent/sessions/--<enc-cwd>--/<ts>_<uuid>.jsonl`, names via `session_info`), `cursor.py`
(`~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` plus the chat store
`~/.cursor/chats/<md5(cwd)>/<id>/`), and read-only `opencode.py` (rows in
`$XDG_DATA_HOME/opencode/opencode.db`).

## How it works

### The two data carriers

- **`RawRef`** — what `discover()` yields: `source`, a stable source-owned `source_ref`, an
  optional filesystem `file`, `mtime`, `size_bytes`, and an optional `fingerprint`. JSONL
  sources use the transcript's session id as `source_ref` and retain its path separately;
  OpenCode uses its session row id and has no file. File discovery stats each file; Codex
  and Droid also read the first record to drop subagent threads, memoized per stat
  fingerprint on the source instance. Each `discover()` pass keeps only the paths it saw, so a
  deleted or moved transcript drops out. The memo pays off only when the same instance scans
  again: `scanner.SourcePool` hands a long-lived caller (`refresh_registry(pool=...)`) the
  instance it used last time for every source whose configuration is unchanged.
  Database sources may query row metadata to produce a per-session fingerprint.
- **`NormalizedSession`** — what `parse()` returns. A cross-harness core (`session_id`, `cwd`,
  `custom_title`, `summary`, `last_prompt`, …) plus Claude-Code-observed extras
  (`message_count`, `git_branch`, `ai_title`) that earn their place by driving real behavior:
  `message_count` gates prune eligibility, the others are display and classification context. A
  source that cannot supply them leaves the defaults.

`tail_text` is transient — used for classification, never persisted — and every source caps it
at 6000 characters. `git_checked_at` is stamped by the scanner, not the source: when git last
gave a definite answer for the cwd.

### The required protocol

```
class Source(Protocol):
    name: str
    capabilities: frozenset[str]
    def discover(self) -> Iterator[RawRef]: ...
    def parse(self, ref: RawRef) -> NormalizedSession: ...
```

Registered stores implement `StoredSource`, which adds a `root: Path` property. A source whose
sessions span several trees also implements `SplitStoreSource`, whose `store_roots` are what
`delete` and `prune` confine a transcript and its companions to.
`AvailabilitySource` optionally supplies an `available()` probe. `active_sources()` and
`SourcePool` use those contracts; injected `scan(sources=...)` readers can still implement
the minimal `Source` protocol. Recorded native roots are honored for all six agents,
including Droid stores outside the current `FACTORY_HOME_OVERRIDE`.

The two-phase split is what makes incremental scans cheap: `discover()` computes lightweight
refs and `parse()` runs only for refs the registry's fingerprint check rejects.

### Capability protocols

Each is `@runtime_checkable`. Protocols with a named capability require both the string in
`capabilities` and an `isinstance` check; rows marked `—` use structural opt-in alone.

| Protocol | Capability | Methods |
|---|---|---|
| `SessionExclusionSource` | — | `excludes_session(file)` — positive evidence that a cached transcript should leave session discovery |
| `CachedMetadataSource` | — | `refresh_cached_metadata()` — update display fields even on a cache hit |
| `DiscoveryCacheSource` | `seeds_discovery` | `seed_discovery()` — prime discovery from version-matched cached sessions; Codex reuses headers only while the file fingerprint matches |
| `CompanionFileSource` | `companion_files` | `companion_paths()` — the per-transcript state that must travel with it; `companion_destination()` — where one goes when the transcript moves |
| `ConversationRenderingSource` | `renders_conversation` | `render_conversation()` — render a source-owned session without assuming JSONL |
| `NameApplyingSource` | `applies_names` | `apply_name()` — write a name into the agent's own store |
| `PickerHidingSource` | `hides_from_picker` | `hide_from_picker()` |
| `PickerShowingSource` | `shows_in_picker` | `show_in_picker()` |
| `WorkspaceRelocatingSource` | `relocates_workspace` | `storage_for()`, `storage_dir()`, `stored_transcripts()`, `relocate_storage()`, `relocate_session()` |
| `WorkspaceRebindingSource` | `rebinds_workspace` | `rebind_workspace()` |
| `ScopedDiscoverySource` | `discovers_scoped` | `discover_scoped()` — one workspace's sessions plus the registry's known in-scope files, changed since a time, without discovering everything |
| `SessionLaunchingSource` | `launches_sessions` | `resume_argv()`, `fork_argv()`, `launch_argv()` |
| `SessionLaunchingSource` | `forks_sessions` | the same protocol; the string alone says `fork_argv()` works |
| `ForkNamingSource` | `names_forks` | `named_fork_argv()` — a fork the agent names as it creates it; only with `forks_sessions` |

`filesystem_lifecycle` is a capability-only opt-in for sources whose sessions are movable files.
Archive, delete, prune, finish, complete, sweep, and restore check it before constructing a
`Path` or touching disk. Claude Code, Codex, Droid, Pi and Cursor opt in. OpenCode deliberately does
not: its database is read-only to ringleader, so it supports scan, overview, triage, detail,
export and harvest, while lifecycle verbs decline or skip it.

**Companion files** are the per-transcript state an agent keeps *beside* the transcript: Claude
Code's `<sessionId>/` directory of subagent and tool-result payloads, droid's
`<sessionId>.settings.json` and `.settings.json.bak`. Archive, delete, prune, unarchive and
workspace move all go through `scanner.companion_paths()` rather than deriving a sibling path
themselves — a session separated from its own sidecars is a session whose token counts or
tool results are gone. `companion_paths()` returns *candidates* derived from the transcript's
path, not a listing of what exists: callers check existence, and the same derivation names the
destination once the transcript has moved. Archive and unarchive pair each companion with
`scanner.companion_moves()`, which asks the source's `companion_destination()`: beside the
transcript for Claude Code and Droid, and back under `chats/<md5(cwd)>/` for a Cursor chat
restored from the archive, where the chat directory sits beside the archived transcript.

**Picker hiding** exists because Codex keeps a sqlite index separate from the rollout files.
Deleting or moving a transcript there leaves the picker offering a session whose file is gone.
`scanner.hide_from_picker()` clears the entry first and **raises** if the source has an index it
cannot update — callers must not proceed to move the file in that case.

`show_in_picker()` is the inverse, and takes the transcript's **original** path, because the
index still points there. The file has to be back in place before it runs.

**Workspace relocation** exists because Claude Code keys storage by the directory a session was
launched in. Both relocate methods **raise rather than merge** when the destination is occupied;
the caller checks up front and refuses, so a move never half-completes. `storage_dir()` is the
inverse of `storage_for()`: the workspace directory a transcript sits under. Drift detection and
workspace moves compare it rather than the transcript's parent, which for Cursor is the chat's own
`agent-transcripts/<id>/` folder. `stored_transcripts()` lists every transcript in such a
directory, which is how a workspace move decides it owns the whole directory.

**Workspace rebinding** is the other shape the same problem takes. Codex files rollouts by date
and records the workspace in a `cwd` column of its state db, which `codex resume` filters the
picker on — so a move relocates nothing and rewrites that one entry instead. Nothing can collide
and no transcript path changes, which is why it is a separate protocol rather than a branch
inside the relocating one. Miss it and the session stays reachable only by its id.

**Session launching** is what `rl resume` and `rl start` dispatch through. `resume_argv()`
re-enters an existing session; `fork_argv()` creates and launches a native fork; `launch_argv()`
starts a fresh one. Its default `prompt=None` returns the bare agent command for `rl start`;
an explicit prompt preserves the seeded cross-agent handoff, including an explicitly empty
string. OpenCode uses `--prompt` and Pi uses `--` for that prompted form.
All return argv whose first element is the binary, and none carries a directory — every agent
resolves a session id relative to the directory it is launched in, so the caller chdirs first and a path in argv would be a second, conflicting answer.
All six sources opt in, including OpenCode, whose read-only store does not stop its CLI being
launched. `forks_sessions` is separate because Cursor has no fork verb: its `fork_argv()` raises,
the resume picker offers no fork or worktree row for it, and `rl resume` refuses one.
`names_forks` marks an agent whose fork command takes a name (Claude Code and Pi pass
`--name`); the picker's naming step and `rl resume --fork`'s numbered name check it through
`resume.names_forks()` and build argv with `named_fork_argv()`, never by agent name. `worktrees.py` composes worktree flags and preparation around these argv builders;
[resume and fresh starts](resume.md) describes the shared launch boundary.

All three index writers (`hide_from_picker`, `show_in_picker`, `rebind_workspace`) return an
**`IndexUpdate`**, not a bool: `NO_INDEX` (this source keeps no index here) and `NO_ENTRY` (it
does, and the session is not in it) need opposite handling from the caller, and
`ALREADY_CURRENT` is success on a re-run rather than a failure to report.

### Helpers

- `agents.source_of(record)` — the shared source lookup for live records and tombstones:
  explicit source, embedded thread source, then the legacy Claude Code default.
- `stat_fingerprint(st)` — `dev:inode:size:mtime_ns:ctime_ns`, the file-backed sources' cache key.
- `append_jsonl_line(path, line)` — appends a record, inserting a leading newline only when the
  file does not already end with one. Writing a name into a live transcript must not corrupt it.

## Key entry points

- `sources/base.py` — every protocol and both dataclasses.
- `scanner.SOURCE_TYPES` — the registered implementations.
- `scanner.active_sources()` — those whose root exists and passes its availability check.
- `scanner.source_by_name()` — resolve a thread's `source` string back to an instance.

## Gotchas

- **`scan()` and `active_sources()` build fresh instances by default.** Storage roots
  reflect the current environment. Long-lived callers use `SourcePool` to retain caches
  only while a source’s configuration remains unchanged.
- **A missing capability is a no-op, not an error.** `hide_from_picker()` returns
  `IndexUpdate.NO_INDEX` for a source without an index. Only a source that *has* one and fails
  raises.
- **Droid has its own directory encoding.** `slug.py` reproduces Claude Code's private slug
  function for Claude Code itself, and a separate `droid_slug()` for droid: `realpath`, then every
  `/` becomes `-`, keeping `_`, `.`, and spaces that Claude Code's encoder would also replace.
  `DroidSource.storage_for()` calls it rather than `slugify()`, and `scanner.storage_slug()`
  (drift detection, workspace-move previews) asks the source's `storage_for()` instead of
  dispatching on its name — verified against 438/438 real droid sessions. Claude Code's
  path goes through `slug.normalize()` (`os.path.abspath(os.path.expanduser(...))`), which
  deliberately does not resolve symlinks — a resolved path would not match the cwd the agent
  recorded. Only `droid_slug()` calls `os.path.realpath`. A symlinked launch dir therefore still
  makes the two encoders diverge; the computed slug simply not existing shows up as a `drifted`
  badge and a no-op `relocate_storage()` rather than as anything overwritten. An upgrade to
  *either* CLI can change its own encoding, so re-verify against both.
- **Droid files transcripts in three places.** A project directory keyed by the launch dir's slug,
  the global root (`sessions/<uuid>.jsonl`), and `sessions/btw/` for a fork. `discover()` reads the
  first two and skips forks, which are never re-filed under a slug. A session at the
  global root has no slug to compare a cwd against, so `resume.drifted()` never badges one.
- **Droid exec runs have a sidecar tag.** Discovery reads `.settings.json` tags independently
  of the memoized transcript header, caching tags by the sidecar's own stat fingerprint.
  Sidecar-only changes take effect, and unknown transcripts are reconsidered even if old.
  Only a tag object with `name: exec` excludes a run; missing, unreadable or malformed settings retain it.
  Refresh also checks cached entries through `SessionExclusionSource`, so partial scans can
  retire a previously discovered exec run within their requested scope. A formerly excluded
  session becoming eligible triggers rediscovery of its workspace, including known displaced
  files. Its transcript and usage data remain on disk.
- **Droid's title is on line 1, so it declines `applies_names`.** `session_start` carries `title`
  and there is no later override record; renaming would mean rewriting the first line of a file
  the agent may still be appending to.
- **A Cursor session spans two roots.** The chat store sits under `CURSOR_CONFIG_DIR` (else
  `$XDG_CONFIG_HOME/cursor`, else `~/.cursor`) and the transcript under `CURSOR_DATA_DIR` (else
  `~/.cursor`). The transcript is the session file because its name, `<id>.jsonl`, is unique, and
  archive names files by name; every chat holds a `meta.json`. Its `store_roots` are the
  `projects/` and `chats/` directories, so `delete` and `prune` accept the chat store wherever
  `XDG_CONFIG_HOME` puts it and refuse anything outside the two. Archive and delete leave the empty
  `agent-transcripts/<id>/` folder behind.
- **Cursor's slug is lossy and the md5 is not.** `slug.cursor_slug()` maps every
  non-alphanumeric run to one `-`, so `/a/b-c` and `/a/b/c` share a `projects/` directory.
  `relocate_storage()` therefore refuses a slug directory holding a transcript whose chat is not
  under the old cwd's md5, rather than carrying another directory's sessions along.
- **Cursor's title lives outside the transcript.** `meta.json` gains `title` after the first turn
  without the transcript changing, so `CursorSource` implements `refresh_cached_metadata()` and
  reports it as `ai_title`. There is no rename verb, so it declines `applies_names`.
- **`parser_version` is the cache-invalidation lever.** Change how a source parses and bump it,
  or every existing thread keeps its stale cached parse.
- **OpenCode fingerprints are per session, not per database file.** They cover the session's
  parsed metadata plus message/part counts, byte sizes and timestamp generations, so changing one
  conversation invalidates one cache entry without transferring every payload during discovery.
  Byte sizes come from `octet_length()`, which reads the record header; `length(CAST(... AS
  BLOB))` gives the same number but loads every payload page, and is only the fallback for
  SQLite older than 3.43.
- **Unavailable stores retain their cached sessions.** A locked or schema-incompatible store is
  omitted from that scan, but its previous registry records are not mistaken for deleted rows or
  classified without a readable transcript tail.
- **`git_resolved` marks a definite git answer from this scan** — the registry then replaces
  the git fields outright, clearing them when the cwd is no longer a repo. When it is false
  (identity reused, cwd gone, lookup failed) the registry only fills in non-empty values.

## Why

Capability protocols rather than one fat interface with optional methods: a source declares only
what it can do, the type checker verifies the call sites, and adding an agent whose store has a
quirk nobody anticipated means adding a protocol rather than widening every implementation.

## Related

- [0003: the Source seam](../decisions/0003-scan-behind-a-source-seam.md)
- [reference/agent-session-management.md](../reference/agent-session-management.md) — the
  external behavior these protocols model, verified per agent and version
- [registry.md](registry.md) — how `NormalizedSession` lands in state

## Deep session portability

`Source` remains the cheap discovery/metadata seam. Full conversations, native
writers, launch argv, roots, index publication and database registration live in
independent capabilities under `portability/`; they are resolved by its adapter
registry. OpenCode source locators may point at copied schema-compatible databases
without weakening target publication layout rules.

Imported threads carry their native store in `nativeRoots`. Normal discovery scans
those stores alongside current environment defaults; lifecycle, naming, rendering
and workspace rebinding resolve the recorded store for each thread. Changing
`CODEX_HOME` after an import therefore neither loses the imported session during
reconciliation nor directs picker updates into the current home's database.

See
[session portability internals](session-portability.md) and
[verified external formats](../reference/session-portability-native.md).
