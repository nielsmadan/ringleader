# session-name — Design Spec (Phase 2, part 1)

**Date:** 2026-06-10
**Status:** Approved design, pre-implementation
**Depends on:** Phase 1 (registry with `suggestedName` + `title` + git-project fields, `sessiontracker/` package, `bin/` CLIs)

## One-line

Apply the registry's `suggestedName`s to dormant Claude Code sessions by appending a `custom-title` record to each transcript — **scoped per repo/dir** — so the picker shows good names and `claude --resume <name>` works. Plus a `st-projects` view to see which repos/dirs have sessions.

## Where "done" detection lives (not here)

This feature does **not** decide which sessions are done. That's already `session-sync`'s job (Phase 1): it classifies each session `finished / unfinished / blocked` and derives `abandoned`. `session-name` only *applies names* to sessions sync has already classified — it consumes `suggestedName`, never re-derives status. So the pipeline is: **classify (sync) → apply names (name)**, both scopeable to a repo/dir.

## Problem

After `session-sync`, each classified session has a `suggestedName` in `registry.json`, but that name lives only in our registry — the real session is still unnamed in Claude Code. Of ~233 sessions, only ~47 carry a user `custom-title`; the rest show up in the picker as an auto `ai-title` or first-prompt blob. We want to push the good names onto the real sessions, in bulk, automatically.

## Mechanism (grounded empirically)

Claude Code stores a session's name as a record `{"type":"custom-title","customTitle":"<name>","sessionId":"<uuid>"}` inside the transcript `.jsonl`. **Last occurrence wins.** Verified facts:

- CC writes this record **constantly itself** — renamed transcripts contain *dozens* (one had 187), scattered throughout the file. Appending one more at EOF is identical to CC's own behavior, not a hack.
- The record has exactly three fields: `type`, `customTitle`, `sessionId`. Nothing else.
- Appending it to a copy keeps every line valid JSON and our scanner reads the new title back correctly.
- Picker label priority is `custom-title → ai-title/summary → first prompt`, so a `custom-title` overrides the auto `ai-title`.

So **naming = append one `custom-title` line to the session's `.jsonl`.** No LLM step (names already exist from `session-sync`); this feature is fully deterministic.

> Not headlessly verifiable here: that CC's interactive picker surfaces the appended title (no TTY). Given CC writes identical records itself with last-wins semantics, this is expected; the skill includes a one-time "resume one and confirm the name shows" check on first use.

## Decisions (locked)

- **Approach:** direct append into the `.jsonl` (approved over assisted-manual / resume-and-rename).
- **No backups, no undo journal** — names are reversible (rename again; last-wins). We try it and revisit if names prove wrong too often.
- **Auto-apply** — write all eligible suggested names in one sweep; no per-session approval gate. (`--dry-run` exists for preview but apply is the default.)
- **Collision handling:** when a name would collide, append a short suffix (first 6 hex chars of the sessionId), mirroring CC's own `name-598df3` style, so `claude --resume <name>` stays unambiguous.

## Eligibility — name and re-name

Sessions get re-used and re-focused, so a name set earlier can go stale. We both **name** newly-classified sessions and **re-name** ones whose focus drifted — but only ones *we* named; never a name the user set by hand.

**Provenance** (registry fields, written by `st-name`, preserved by reconcile, never auto-overwritten):
- `appliedName` — the last title *we* wrote (None until we name it).
- `namedAtMtime` — the file mtime when we wrote it.

A thread is a **candidate** when: `suggestedName` is non-empty, `status != "unclassified"`, and it is **not active** (file `mtime` older than `active_window_minutes`, default 10 — avoids racing a session CC is writing). Among candidates, `kind`:

| Condition | `kind` | Action |
|---|---|---|
| `title` empty | **new** | write `suggestedName` |
| `title` non-empty, `title == appliedName`, `suggestedName != title` | **rename** | the session drifted since we named it → write the fresh `suggestedName` (last-wins overrides our old record) |
| `title` non-empty, `title != appliedName` | *(skip — user-owned)* | never touch a name the user set |
| `title` non-empty, `title == appliedName`, `suggestedName == title` | *(skip — converged)* | already accurate; nothing to do |

**Churn is bounded.** A fresh `suggestedName` only appears after `session-sync` re-classifies, and sync only re-classifies when the file's mtime advanced (the session was genuinely re-used). So unchanged sessions are never re-named; once re-named, `title == appliedName == suggestedName` so it re-converges until the next reuse.

## Collision handling

Build the set of "taken" names = all existing non-empty `title`s across the registry, plus names already assigned in this batch. For each eligible thread, the final name is:
- `suggestedName` if not taken, else
- `f"{suggestedName}-{sessionId.replace('-','')[:6]}"` (deterministic, effectively unique per session).

The assigned final name is added to the taken set so later threads see it.

## Scoping (repo / dir) and the projects view

The whole point is to roll out per repo, not in one big sweep across 233 sessions. Two pieces:

### Selector semantics — `--project <selector>`
A `selector` matches a thread when it equals (case-insensitive) any of: `gitProjectLabel`, `gitProjectKey`, `gitRoot`, or `project` (cwd) — or is a path **prefix** of `gitRoot`/`project`. So one selector can target:
- a **git project** (e.g. `--project acme-core`) → groups every cwd that resolves to that repo, even across sibling dirs/worktrees;
- a **single dir** (e.g. `--project /Users/you/wrksp/acme/backend`).

`--project` is **repeatable** (name several repos at once). Omitting it means "all" (the current unscoped behavior).

The selector applies to **both ends of the pipeline**:
- `bin/st-sync scan --project <selector>` — reconcile the whole registry as usual (cheap), but only *emit* the pending-classification list for the selected scope, so the classifier (the skill agent) only works on that repo/dir.
- `bin/st-name --project <selector>` — only apply names within the selected scope.

So the per-repo workflow is: **`st-projects` (pick) → `st-sync scan --project X` (classify X) → `st-name --project X` (name X)**.

### `bin/st-projects` — the menu (read-only)
Lists every project we have sessions for, so you can decide what to scope to. Groups non-tombstoned threads by `gitProjectKey` (fallback cwd), and lists non-git cwds separately. Per group shows: label, key (remote/root), **total** sessions, **classified**, **unnamed** (classified + no `custom-title` = nameable now). Sorted by total desc. Pure function `project_summary(reg) -> list[ProjectStat]` in `sessiontracker` (unit-tested); `bin/st-projects` is the thin entrypoint. Surfaced via the `session-overview` skill (a "list projects" capability) so it's reachable conversationally.

## Components

### `sessiontracker/naming.py` (pure, unit-tested)
- `candidate_kind(thread, now_iso, active_window_minutes=10) -> "new" | "rename" | None` — the eligibility table above (uses `overview.age_days` for the active check). Returns None for non-candidates and the two skip cases.
- `unique_name(suggested, session_id, taken) -> str` — returns `suggested` or the suffixed form.
- `title_line(session_id, name) -> str` — the exact JSON line (no trailing newline; the writer adds it).
- `select_eligible(reg, now_iso, active_window_minutes=10) -> list[tuple[thread, final_name, kind]]` — filters to candidates with `kind in {new, rename}` (deterministic order: by `mtime`), assigns unique names (taken-set seeded from existing titles), returns `(thread, final_name, kind)`. No side effects.

### `sessiontracker/projects.py` (pure, unit-tested)
- `matches(thread, selector) -> bool` — the selector semantics above (equality on label/key/root/cwd, or path-prefix on root/cwd). Shared by `st-sync`, `st-name`, and `st-projects`.
- `filter_by_projects(threads, selectors) -> list[thread]` — keep threads matching any selector (all if `selectors` empty).
- `project_summary(reg) -> list[ProjectStat]` — per-group counts (total / classified / unnamed), git groups then non-git cwds, sorted by total desc.

### `bin/st-name` (CLI, thin side-effecting wrapper)
Flags: `--registry`, `--dry-run`, `--project <selector>` (repeatable; selector semantics above), `--limit N`, `--active-window <min>` (default 10).

Apply flow:
1. Load registry; compute `now` at the edge.
2. `selected = select_eligible(reg, now, active_window)`, then apply `--project` (via `filter_by_projects`) / `--limit` filters.
3. **`--dry-run`:** print the table — `new: <name>` and `rename: <old> → <new>` rows, each with session (whatItIs, project) — and exit; write nothing.
4. Otherwise, for each `(thread, final_name, kind)`:
   - Append the title line to `thread["file"]` (ensure the file ends with a newline first, then write `title_line + "\n"`). For a **rename** this is just another `custom-title` record at EOF — last-wins overrides the prior one; we don't rewrite the file.
   - Set `thread["title"] = final_name`, `thread["appliedName"] = final_name`, `thread["namedAtMtime"] = thread["mtime"]` in the registry.
   - On `OSError`, warn to stderr and skip that one (never abort the batch — same pattern as the scanner).
5. Save the registry. Print a summary: new N, renamed R, skipped M, suffixed K.

### `bin/st-projects` (CLI, read-only)
Flags: `--registry`. Prints the `project_summary` table (label · total · classified · unnamed), git groups first, non-git cwds after. No writes.

### `bin/st-sync` change (small Phase-1 touch)
Add `--project <selector>` (repeatable) to the `scan` subcommand. It does **not** change reconciliation (still whole-registry); it only filters the emitted `pending` list through `filter_by_projects`, so a scoped sync classifies just that repo/dir.

### Skills (conversational entry)
- `.claude/skills/session-name/SKILL.md`: orchestrates a **scoped** run — `st-projects` to pick a repo → `st-sync scan --project X` (classify X) → `st-name --project X --dry-run` to preview → `st-name --project X` to apply → report. First-use note: resume one named session (or open the picker) and confirm the new name shows.
- `.claude/skills/session-overview/SKILL.md` (extend): add a "list projects" capability that runs `st-projects`, so "which repos do I have sessions for?" is answerable.

## Edge cases & robustness

- **Trailing newline:** if a target file doesn't end with `\n`, write a leading `\n` before the record so it lands on its own line.
- **Active file race:** the `active_window` skip is the guard; appends are single short line-writes, but we still avoid recently-touched files.
- **Re-runs:** idempotent via rule 3 (named → has `title` → skipped).
- **Per-file failure:** caught, warned, skipped.

## Registry additions

Two fields on each thread, written by `st-name` and **preserved by `reconcile`** (same treatment as `decision`/`harvestPath` — never overwritten by metadata refresh): `appliedName`, `namedAtMtime`. New threads default both to `null`.

## Non-goals (this slice)

- Refreshing a name the **user** set by hand (we only re-name names we generated).
- `/clear` multi-thread files get a single whole-file title (matches CC's own model).
- Harvest / prune / todo-linking — separate Phase 2 slices.

## Testing

TDD the pure layer (`naming.py`): `candidate_kind` for each row of the eligibility table — **new** (empty title), **rename** (title == appliedName, suggestedName drifted), skip-user-owned (title != appliedName), skip-converged (title == appliedName == suggestedName), skip-active (recent mtime), skip-unclassified; `unique_name` collision suffixing; `title_line` exact format; `select_eligible` ordering, taken-set, and `kind` tagging. Also: after a **rename**, the thread re-converges (a second `select_eligible` no longer returns it), and a user-set title (title != appliedName) is never selected. Registry: `reconcile` preserves `appliedName`/`namedAtMtime`. TDD `projects.py`: `matches` (label/key/root/cwd equality + path-prefix, case-insensitive, and non-matches), `filter_by_projects` (any-match, empty=all), `project_summary` (counts + ordering + non-git bucket). TDD `bin/st-name` against temp `.jsonl` files: line appended correctly, registry `title` updated, idempotent second run skips, `--dry-run` writes nothing, `--project`/`--limit` scoping, trailing-newline handling, per-file OSError skipped. TDD the `st-sync scan --project` filter (emitted pending respects scope). Smoke-test `bin/st-projects` against a temp registry.
