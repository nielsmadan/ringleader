# Registry

`registry.json`: the single state file, its schema, and the reconcile contract that keeps it
honest against disk.

## Purpose

`registry.py` owns every read and write of the registry. It is a **cache of disk reality plus a
record of decisions** — see [0001](../decisions/0001-hybrid-registry-as-cache-not-master.md) for
why that split exists. Everything above it (overview, triage, prune, the commands) mutates the
registry only through functions in this module; nothing else edits the dict shape.

Location is resolved by `paths.default_registry()` —
`~/.local/share/ringleader/registry.json`, or `$XDG_DATA_HOME/ringleader` when set. Never in
the repo ([0010](../decisions/0010-keep-machine-local-state-outside-the-repo.md)).

## How it works

### Concurrent mutations

`load_registry()` returns a dict-compatible snapshot with a private baseline.
After bounded edits, `commit_registry()` locks `<registry>.lock`, reloads the file and
applies the changes relative to that baseline. Session rows match by `(source, sessionId)`,
memory rows by path, and nested mappings by field. Unrelated concurrent edits survive;
conflicting edits to the same field raise `RegistryConflict` before any registry write.
A successful commit refreshes the caller's snapshot and baseline. A commit or
`mutate_registry` whose result equals what it loaded from disk skips the write and its two
fsyncs. A load parses the file once; the baseline is parsed from the same text only when
something first reads it (a commit, or a mutation's unchanged check), and after a write it is
parsed lazily from the text just written. A second parse is the cheaper independent copy: on a
~4.7 MB registry a parse is about 17 ms against about 20 ms for a `deepcopy`. `save_registry()` is
for initial plain-dict fixtures; production writers use the explicit commit API.
Session merges use ordered identity maps, keeping bulk scan updates linear in the
number of rows. Loading removes stale deleted tombstones for identities that also
have live or archived rows before capturing the baseline; old GC duplicates cannot
mask subsequent decisions or lifecycle changes.

`reconcile()` and `reconcile_memory()` retain baseline provenance. Scan-derived conflicts
keep the newer committed value, pinned locations remain pinned, and scans cannot erase
sessions added while discovery ran or resurrect a concurrently archived session.
Unknown top-level fields and record metadata survive scans. Lifecycle moves retain the
latest complete thread in their tombstone, including concurrent lineage changes. A scan's
GC tombstone can be completed into an archive after its filesystem move finishes.

`mutate_registry(path, callback)` supports short mutations against freshly loaded state;
callback failure writes nothing. The inbox's writer uses this for interactive metadata changes.
Scans, model calls, confirmation prompts and filesystem moves run outside the registry
lock. Pruning revalidates each candidate against current state and holds the registry lock
through picker hiding, deletion and its tombstone write, so a committed keep decision wins
over an earlier candidate list. The inbox's delete carries the `mtime` and `fileFingerprint` the user saw
into this check, so intervening activity or relocation cancels its deletion. Filesystem
operations are not atomic with the registry; archive retries continue using the existing
archive destination if the registry write failed.

Batched destructive work holds the registry lock across each batch of up to 25
(`prune.DELETE_BATCH`, the one bound): `rl prune --delete`, the roundup's prune and sweep,
and the inbox writer's archives, deletes and marks all go through `batch.run_batch`, which
loads once, runs each item's act against that state right before acting, and writes once. A
single inbox decision is a batch of one, so grouped and ungrouped ops share one transaction
shape. Presence is probed lazily inside the transaction, after the publish and registry locks
are taken, and re-probed before an item once the reading is older than
`batch.PRESENCE_MAX_AGE` (1 s), so a slow move cannot stretch one reading across the batch.
An item that raises is recorded as that item's failure and the batch carries on. A
`KeyboardInterrupt` or `SystemExit` stops the batch after the current item, commits what the
earlier items did, then re-raises. A failed write after a file moved or was unlinked reports
each such item as "on disk, but the registry was not updated" (`BatchRun.settled`).

Lock order is **publish or operation lock, then registry lock**. Never acquire an outer
lock or call another registry transaction inside a registry callback. Both transaction
APIs release the lock on failure. `persist=False` refreshes create no file or lock.

### Top-level shape

`empty_registry()` defines it; `REGISTRY_VERSION` is `2`.

| Key | Holds |
|---|---|
| `threads` | live sessions — the working set |
| `archived` | archived sessions, each with the full thread record for restore |
| `deleted` | tombstones for transcripts that are gone |
| `conversionLineage` | authoritative operation-keyed source/target conversion and fork relations |
| `memoryEntries` | the memory track's parallel working set |
| `projectsCleanedAt` / `memoryProjectsCleanedAt` | per-project "last triaged" stamps |
| `lastScan` / `lastRoundupAt` | staleness signals: `lastScan` feeds the overview's staleness banner (memory scans advance it too), `lastRoundupAt` is what `rl inbox` checks before a roundup |
| `sourcesScannedAt` | per source, when a scan last discovered it in full; each source's baseline for a scoped scan (`rl resume --last`) |

`archived` and `deleted` are deliberately separate lists: an archived transcript still exists
and is restorable, a deleted one does not. Collapsing them would make `unarchive` unable to
tell what it may bring back.

### Thread fields

Three groups, each refreshed under a different rule — this is the part worth knowing before
editing:

- **`_METADATA_FIELDS`** — derived from the source on every scan (`sourceRef`, optional `file`, `project`,
  `title`, `summary`, `lastPrompt`, `mtime`, `sizeBytes`, `messageCount`, `gitBranch`,
  `fileFingerprint`, `cacheVersion`). Refreshed from scans unless a newer commit or pinned location supersedes the scan.
- **`filteredBy`** — the `config.session_filters` pattern this session's name matched, or
  `None`. Stamped on every scan like the metadata fields, and read by the views to hide the
  session ([Filtering noisy sessions](../features/views-and-triage.md#filtering-noisy-sessions)).
- **`_GIT_PROJECT_FIELDS`** — `gitProjectKey`, `gitProjectLabel`, `gitRoot`, `gitRemote`,
  `gitCloneUrl`, and `gitCheckedAt`, the time the cwd was last checked, which the scanner uses
  to skip `git` for a day. These need the **live filesystem**, so a vanished cwd resolves to `None`.
  Reconcile refreshes them **only when the new scan actually resolved a value**, otherwise the
  cached one survives — without that rule, unplugging an external disk would blank the git
  identity of every session on it.
- **Judgment and decision fields** — `status`, `statusConfidence`, `whatItIs`, `suggestedName`,
  `appliedName`, `decision`, `harvestPath`, `tags`, plus the lifecycle stamps
  (`snoozedAt`, `keptAt`, `finishOnExit`, `reviewedAtMtime`, `classifiedAtMtime`,
  `namedAtMtime`) and `locationPinned`. **Never overwritten by a scan.** `selectedForDay` is
  unused: still written as `null` on new sessions, read by nothing.

Conversion fields `lineage` and `nativeRoots` are preserved by scans and lifecycle
transitions. `conversionLineage` retains relationships even after target deletion;
operation journals under `conversion/` supply recovery evidence. See
[session portability](session-portability.md).

A new thread starts at `status: "unclassified"` with the judgment fields empty.

### Identity

`identity()` is `(source, sessionId)`. Archived/deleted records also carry their source;
older tombstones can recover it from their embedded thread. A missing source defaults to
`claude-code` so v1 records keep working. Selectors accept `AGENT:ID` (`claude` aliases
`claude-code`). A bare ID must be unambiguous across live, archived and deleted records.
`SessionIndex(reg)` builds a `sessionId` map once and answers `find`/`session_identity`;
`find_session` is the same lookup over an index of just the selector's `sessionId`, so the
ambiguity rules have one implementation. Loops over many ids share one index, and the bucket
moves (`archive_session`, `prune_session`, `purge_archived_session`) take `index=` and keep it
current themselves; `archive_thread` passes a batch's index through to `archive_session`.
New harvest frontmatter uses a qualified selector in its `sessionId` field; legacy bare
references still work when unambiguous. Native registry `sessionId` values stay unchanged. The session id is the registry's key, which is why
[workspace moves refuse rather than merge](../features/workspace-portability.md) — a merge
would have to rewrite one.

### The cache check

`cached_session()` returns a `NormalizedSession` rebuilt from the registry — skipping the parse
entirely — only when **all** of these hold:

- the thread exists,
- the source reports a `parser_version` and it equals the thread's `cacheVersion`,
- the ref carries a fingerprint and it equals the thread's `fileFingerprint`.

`fileFingerprint` is the historical registry field name for the source-owned fingerprint. JSONL
sources store `stat_fingerprint()` (dev, inode, size, mtime_ns, ctime_ns); OpenCode stores a
per-session metadata/conversation digest. Bumping a source's `parser_version` invalidates every
cached parse for that source at once, which is the intended way to ship a parser change.

### Reconcile

`reconcile(reg, scanned, now_iso, *, unavailable_sources, partial)` is the one function that merges
a scan into the registry:

1. Index existing threads by `(source, sessionId)`.
2. For each scanned session: refresh the metadata group, conditionally refresh the git group,
   preserve everything else. New ids get `_new_thread()`.
3. **Skip any scanned session whose id is in `archived` but absent from `threads`** — an agent
   that wrote after we archived it recreated the transcript, and re-registering would undo the
   archive ([0005](../decisions/0005-archive-by-moving-not-deleting.md)).
4. Threads not seen in the scan move to `deleted`, keeping their decision and harvest pointers —
   unless their source was unavailable, or the scan was `partial` (it looked at only part of each
   source, so not seeing a thread proves nothing). A partial scan moves no clock. Otherwise each
   source the caller names in `scanned_sources` gets `now_iso` in `sourcesScannedAt` — except an
   unavailable one, whose writes meanwhile went unseen.

`now_iso` is passed in rather than read, so reconcile is deterministic and testable. Callers must
read it **before** discovery starts: it becomes each source's baseline, and a file written
mid-scan with an earlier mtime would otherwise be skipped by every later scoped scan.

## Key entry points

- `reconcile()` / `reconcile_memory()` — merge a scan.
- `cached_session()` — the parse-skip decision.
- `apply_classification()` / `apply_name()` — write judgment fields.
- `archive_session()` / `restore_session()` / `purge_archived_session()` — the archive lifecycle.
- `prune_session()` — tombstone a deletion with a reason (`prune`, `deleted`, `gc`, `purged`).
- `snooze_thread()` / `wake_thread()` / `keep_thread()` / `unkeep_thread()` — triage state.
- `mark_finish_on_exit()` / `without_finish_mark()` — the sweep handshake ([0008](../decisions/0008-sweep-archives-only-quiet-transcripts.md)).
- `pin_thread_location()` / `remap_cached_git_root()` — see [workspace moves](#).

## Gotchas

- **`locationPinned` is load-bearing.** A transcript records the cwd it was launched in. After a
  workspace moves, that baked-in value is stale, so a plain scan would revert the registry to
  the old path on the very next run. `pin_thread_location()` sets the flag that makes reconcile
  leave the location alone.
- **Archived ids are checked against `threads`, not just `archived`.** A session can legitimately
  be in both — that is an unarchive in progress — and the skip only applies when it is archived
  *and* absent from the working set.
- **Writes go through `safe_write` / `stateio`,** never a bare `open().write()`: a crash
  mid-write must not leave a truncated registry.
- **The schema is not validated on load.** `load_registry()` parses JSON and returns it; a
  hand-edited registry with a wrong shape fails later, at the point of use.

## Why

One JSON file, rewritten atomically, rather than a database: the state is small (hundreds to
low thousands of threads), it benefits from being readable and hand-fixable, and the tool's
value proposition is that it is *less* infrastructure than the problem it solves.

## Related

- [0001: registry as cache](../decisions/0001-hybrid-registry-as-cache-not-master.md)
- [scanning-and-refresh.md](scanning-and-refresh.md) — what produces the scan reconcile consumes
- [sources.md](sources.md) — where `NormalizedSession` comes from
- [features/session-lifecycle.md](../features/session-lifecycle.md) — the states these fields encode
