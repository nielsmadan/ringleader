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

### Top-level shape

`empty_registry()` defines it; `REGISTRY_VERSION` is `2`.

| Key | Holds |
|---|---|
| `threads` | live sessions — the working set |
| `archived` | archived sessions, each with the full thread record for restore |
| `deleted` | tombstones for transcripts that are gone |
| `memoryEntries` | the memory track's parallel working set |
| `projectsCleanedAt` / `memoryProjectsCleanedAt` | per-project "last triaged" stamps |
| `lastScan` / `lastRoundupAt` | staleness signals, used by `rl inbox` to decide whether to roundup |

`archived` and `deleted` are deliberately separate lists: an archived transcript still exists
and is restorable, a deleted one does not. Collapsing them would make `unarchive` unable to
tell what it may bring back.

### Thread fields

Three groups, each refreshed under a different rule — this is the part worth knowing before
editing:

- **`_METADATA_FIELDS`** — derived from the transcript on every scan (`file`, `project`,
  `title`, `summary`, `lastPrompt`, `mtime`, `sizeBytes`, `messageCount`, `gitBranch`,
  `fileFingerprint`, `cacheVersion`). Cheap and always safe to overwrite.
- **`filteredBy`** — the `config.session_filters` pattern this session's name matched, or
  `None`. Stamped on every scan like the metadata fields, and read by the views to hide the
  session ([Filtering noisy sessions](../features/views-and-triage.md#filtering-noisy-sessions)).
- **`_GIT_PROJECT_FIELDS`** — `gitProjectKey`, `gitProjectLabel`, `gitRoot`, `gitRemote`,
  `gitCloneUrl`. These need the **live filesystem**, so a vanished cwd resolves to `None`.
  Reconcile refreshes them **only when the new scan actually resolved a value**, otherwise the
  cached one survives — without that rule, unplugging an external disk would blank the git
  identity of every session on it.
- **Judgment and decision fields** — `status`, `statusConfidence`, `whatItIs`, `suggestedName`,
  `appliedName`, `decision`, `harvestPath`, `tags`, plus the lifecycle stamps
  (`snoozedAt`, `keptAt`, `finishOnExit`, `selectedForDay`, `reviewedAtMtime`,
  `classifiedAtMtime`, `namedAtMtime`) and `locationPinned`. **Never overwritten by a scan.**

A new thread starts at `status: "unclassified"` with the judgment fields empty.

### Identity

`_key()` is `(source, sessionId)`, defaulting a missing source to `claude-code` so v1 records
keep working. The session id is the registry's key, which is why
[workspace moves refuse rather than merge](../features/workspace-portability.md) — a merge
would have to rewrite one.

### The cache check

`cached_session()` returns a `NormalizedSession` rebuilt from the registry — skipping the parse
entirely — only when **all** of these hold:

- the thread exists,
- the source reports a `parser_version` and it equals the thread's `cacheVersion`,
- the ref carries a fingerprint and it equals the thread's `fileFingerprint`.

`fileFingerprint` is `stat_fingerprint()`: dev, inode, size, mtime_ns, ctime_ns. Bumping a
source's `parser_version` invalidates every cached parse for that source at once, which is the
intended way to ship a parser change.

### Reconcile

`reconcile(reg, scanned, now_iso)` is the one function that merges a scan into the registry:

1. Index existing threads by `(source, sessionId)`.
2. For each scanned session: refresh the metadata group, conditionally refresh the git group,
   preserve everything else. New ids get `_new_thread()`.
3. **Skip any scanned session whose id is in `archived` but absent from `threads`** — an agent
   that wrote after we archived it recreated the transcript, and re-registering would undo the
   archive ([0005](../decisions/0005-archive-by-moving-not-deleting.md)).
4. Threads not seen in the scan move to `deleted`, keeping their decision and harvest pointers.

`now_iso` is passed in rather than read, so reconcile is deterministic and testable.

## Key entry points

- `reconcile()` / `reconcile_memory()` — merge a scan.
- `cached_session()` — the parse-skip decision.
- `apply_classification()` / `apply_name()` — write judgment fields.
- `archive_session()` / `restore_session()` / `purge_archived_session()` — the archive lifecycle.
- `prune_session()` — tombstone a deletion with a reason (`prune`, `deleted`, `gc`, `purged`).
- `snooze_thread()` / `wake_thread()` / `keep_thread()` / `unkeep_thread()` — triage state.
- `mark_finish_on_exit()` / `without_finish_mark()` — the sweep handshake ([0008](../decisions/0008-sweep-archives-only-quiet-transcripts.md)).
- `pin_thread_location()` / `relocate_thread()` — see [workspace moves](#).

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
