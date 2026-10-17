# Scanning and refresh

How a run gets from stored agent sessions to a reconciled registry: `scanner.py`,
`refresh.py`, `gitinfo.py`, `projects.py`, `slug.py`.

## Purpose

`scanner.scan()` walks every active source, skips the parse wherever the registry's cache is
valid, attaches git identity, and returns a list of `NormalizedSession`. `refresh.auto_refresh()`
wraps that with reconcile-and-persist, and is what `overview`, `triage` and `roundup` call
before rendering.

## How it works

### The scan loop

`scan(sources, git_resolver, cached_threads, *, refs_of, resolve_cached_git)`:

1. Index `cached_threads` by `(source, sourceRef)`, falling back to `file` for registries written
   before `sourceRef` existed.
2. For each source, for each `discover()` ref — or `refs_of(source)`, when a caller narrows
   discovery: ask `registry.cached_session()` whether the cache
   is valid. On a hit, skip `parse()` entirely; on a miss, parse and stamp the result with the
   source's `parser_version` and the ref's fingerprint.
3. On a cache hit, a `CachedMetadataSource` still gets `refresh_cached_metadata()` — display
   fields that changed without the transcript changing.
4. Resolve git identity for the session's cwd, **memoized per cwd** in a local dict, so a repo
   with forty sessions shells out to git once. `resolve_cached_git=False` skips it for cache hits,
   which keep the identity the registry already holds.
5. A read failure on one transcript or source-owned row prints to stderr and skips it — one
   unreadable session store must not abort scans of unrelated sources. If an active store is
   temporarily unavailable, reconciliation retains its cached sessions instead of marking them
   gone.

Both `sources` and `git_resolver` are injectable, which is how the tests run without touching a
real store.

### Git identity

`gitinfo.resolve_git_project(cwd)` returns a `GitProject` (`key`, `label`, `root`, `remote`,
`clone_url`) or `None`. The **key** is what collapses worktrees, clones and monorepo subdirs of
the same repo into one row in the overview: a normalized remote when there is one, else the repo
root path. `normalize_remote()` reduces both SSH and HTTPS forms to `host/org/repo`.

Sessions with no resolvable git project fall back to grouping by cwd.

### Selectors

`projects.matches(thread, selector)` implements the `<selector>` accepted across the CLI: a git
project **label** (`ringleader`) or a **cwd path**. Never a session id. `filter_by_projects()`
applies a repeatable list, and `project_summary()` builds the `ProjectStat` rollup the overview
renders.

### The Claude Code slug

`slug.py` reproduces Claude Code's project-directory encoding, decompiled from the binary:
non-alphanumerics become `-`, and a result over `SLUG_MAX_LEN` (200) is truncated and suffixed
with a base-36 rolling hash (`h = (h << 5) - h + charCode | 0`, truncated to a signed 32-bit int
at each step).

Ringleader needs this to find and move a session's storage directory during a workspace move.
The rest of the module is path arithmetic that must match what the agent recorded:

- `normalize()` — absolute and lexically normalized, **without resolving symlinks**, because a
  resolved path would not match the cwd the agent wrote down.
- `is_beneath()` — lexical containment respecting directory boundaries, so `/a/b-two` is not
  treated as inside `/a/b`. Sibling checkouts of one repo often differ by exactly that suffix.
- `remap()` — rewrite a path from under one root to under another.

### Refresh

`auto_refresh()` scans, reconciles, persists, and returns what changed. `delta_note()` renders
the one-line summary (`+3 new, -1 gone`) or `None` when nothing moved. Counts are refreshed on
every view so they are never stale; classification and naming stay separate, agentic steps.

`scoped_refresh(cwd, recursive)` is the cheap variant `rl resume --last` uses, where a full scan
(~20s on a large machine) would dominate. It rescans only what changed since each source's entry
in `sourcesScannedAt` and could belong to `cwd`, through `scanner.scoped_refs()`:

- a `ScopedDiscoverySource` narrows itself — Claude Code and droid read the workspace's storage
  dir, Codex drops rollouts recorded elsewhere before parsing them — and also takes the
  registry's own in-scope files (`known`), which is how a drifted or relocated session is found;
- any other source discovers everything and is filtered on mtime.

It reconciles with `partial=True`, so nothing it skipped is tombstoned and no clock moves, and
skips git for cache hits. A source with no baseline yet is discovered whole; a registry where no
source has one gets the full scan instead, and says so in its return value so `rl resume --last`
does not scan everything twice. Both refreshes share one private `_refresh()` pipeline.

### Roundup

`roundup.py` is the roundup as a function API, so the CLI and the inbox share one
implementation. `plan_roundup()` takes a registry the caller already scanned and names the
exact identities each phase may touch: the sweep set (`sweep.due`), the prune set
(`prune_candidates` minus the sweep set), the purge set, and the enrich sets (pending
classification minus sweep and prune, newest first, plus the subset active within
`RECENT_DAYS`). Each sweep and prune entry is a `revalidate.Seen` — the identity, label,
registry `mtime` and `fileFingerprint` at planning time.

`run_phases()` holds the non-blocking `stateio.roundup_lock()` (`RoundupBusy` when another
roundup has it) and executes only what the plan named, rechecking each item first via
`revalidate.py`. Sweep archives one session per `publish_lock` and commits each separately, so
a later conflict cannot undo an earlier archive; it skips a session no longer marked
`finishOnExit`, one written within `sweep.GRACE_SECONDS`, or one presence reports running.
Prune (`prune_planned`) works in batches of `PRUNE_BATCH`, probing presence once per batch and
skipping any session whose registry `mtime`/`fileFingerprint` moved since the plan;
`prune._delete_candidate` adds the transcript `(size, mtime_ns)` check, which `rl prune
--delete` gets too. `cancelled()` is polled between items and batches. `run_roundup()` composes
phases, `enrichment.enrich_sessions()` over the chosen scope, and `stamp()` of
`lastRoundupAt`, which is skipped only when the run was cancelled. `next_step()` is that
sequencing rule (phases → enrich or stamp, never stamp a cancelled run) and `hold_lock()` the lock;
`run_roundup()` and the inbox's `RoundupDriver` both call them.

A presence probe returning `None` means "cannot tell". The caller picks what that means:
`rl roundup` passes `unknown="allow"` (machines without presence keep working), while the inbox
refuses. The inbox runs the plan's phases, enrich and stamp itself, one writer op or job each,
and holds the roundup lock through all of them.

## Key entry points

- `scanner.scan()` — the loop above.
- `refresh.auto_refresh()` — scan + reconcile + persist; what `--no-scan` skips.
- `refresh.scoped_refresh()` / `scanner.scoped_refs()` — the scoped rescan behind `--last`.
- `roundup.plan_roundup()` / `run_phases()` / `run_roundup()` — the roundup API;
  `revalidate.py` — the per-item checks it runs before touching a session.
- `gitinfo.resolve_git_project()` — cwd → `GitProject`.
- `projects.matches()` — the selector contract.
- `slug.slugify()` — cwd → Claude Code's directory name.

## Gotchas

- **`--no-scan` reports the registry as-is.** Useful for a fast read or a deterministic test;
  wrong if you need current counts.
- **Scanning dominates runtime** on a large `~/.claude/projects`. `overview` and `triage` both
  scan by default.
- **`slug.py` is a reimplementation of someone else's private function.** It is correct as of the
  versions recorded in [reference/agent-session-management.md](../reference/agent-session-management.md)
  and can break when Claude Code changes it. Re-verify there before trusting it after an upgrade.
- **`normalize()` must not resolve symlinks.** This looks like a bug and is not — see above.

## Why

Two-phase discover/parse plus a source-owned fingerprint avoids reparsing unchanged sessions.
For JSONL that costs one `stat()` per transcript; OpenCode derives per-session generations from
row counts, byte sizes and update timestamps, then reads conversation payloads only on cache
misses. Memoizing git per cwd matters more than it looks: `resolve_git_project` shells out,
and a machine with hundreds of sessions across a dozen repos would otherwise pay hundreds of
subprocess spawns per view.

## Related

- [sources.md](sources.md) · [registry.md](registry.md)
- [features/workspace-portability.md](../features/workspace-portability.md) — the slug's consumer
