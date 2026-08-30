# Scanning and refresh

How a run gets from "transcripts on disk" to "a reconciled registry": `scanner.py`,
`refresh.py`, `gitinfo.py`, `projects.py`, `slug.py`.

## Purpose

`scanner.scan()` walks every active source, skips the parse wherever the registry's cache is
valid, attaches git identity, and returns a list of `NormalizedSession`. `refresh.auto_refresh()`
wraps that with reconcile-and-persist, and is what `overview`, `triage` and `roundup` call
before rendering.

## How it works

### The scan loop

`scan(sources, git_resolver, cached_threads)`:

1. Index `cached_threads` by `(source, file)`.
2. For each source, for each `discover()` ref: ask `registry.cached_session()` whether the cache
   is valid. On a hit, skip `parse()` entirely; on a miss, parse and stamp the result with the
   source's `parser_version` and the ref's fingerprint.
3. On a cache hit, a `CachedMetadataSource` still gets `refresh_cached_metadata()` — display
   fields that changed without the transcript changing.
4. Resolve git identity for the session's cwd, **memoized per cwd** in a local dict, so a repo
   with forty sessions shells out to git once.
5. `OSError` on a single transcript prints to stderr and skips that file — one unreadable
   transcript must not abort the scan.

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

## Key entry points

- `scanner.scan()` — the loop above.
- `refresh.auto_refresh()` — scan + reconcile + persist; what `--no-scan` skips.
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

Two-phase discover/parse plus a stat fingerprint means the steady state costs one `stat()` per
transcript. Memoizing git per cwd matters more than it looks: `resolve_git_project` shells out,
and a machine with hundreds of sessions across a dozen repos would otherwise pay hundreds of
subprocess spawns per view.

## Related

- [sources.md](sources.md) · [registry.md](registry.md)
- [features/workspace-portability.md](../features/workspace-portability.md) — the slug's consumer
