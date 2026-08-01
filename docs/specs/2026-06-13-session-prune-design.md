# session-prune — Design Spec (curation, part 1 of 2)

**Date:** 2026-06-13
**Status:** Approved design, pre-implementation
**Depends on:** Phase 1 + 2 (registry with `decision`/`sizeBytes`/`messageCount`, `sessiontracker/` package, `bin/st-*` + `./st` dispatcher)

## One-line

Bulk-delete clearly-useless dormant sessions — tiny *and* stale transcripts — deliberately and with a preview, removing the `.jsonl` + sidecar dir and tombstoning them in the registry.

## Scope & decomposition

This is **part 1 of a two-part curation flow**. Part 2 (its own brainstorm/spec next) is the interactive **triage → harvest → archive**: go session-by-session, leave/rename the live ones, and for finished ones extract knowledge into docs then archive/mark. This spec covers **only prune** (the mechanical bulk-delete). Triage/harvest/archive are explicit **non-goals** here.

`cleanupPeriodDays` is already set high, so nothing auto-deletes; prune is the deliberate replacement for time-based GC.

## Problem

The registry holds ~270 sessions; many are tiny abandoned stubs (a few KB, days old) cluttering the overview and disk. The status labels are unreliable (a quick Haiku tail-pass), so we don't trust them for deletion. But "tiny **and** old" is a robust, label-independent signal of junk worth removing in bulk.

## Decisions (locked)

- **Candidate rule:** `age ≥ older_than_days` **AND** (`sizeBytes < max_size` **OR** `messageCount < max_turns`). **Status-agnostic.**
- **Only protection:** sessions with `decision == "keep"` are never pruned. (`abandon`/`harvested`/`null` are all eligible — harvested means knowledge is already saved.)
- **Defaults (tunable via flags):** `--older-than 7` (days), `--max-size 50` (KB), `--max-turns 12` (where "turns" = `messageCount`, the count of user+assistant records).
- **Preview-by-default; delete is opt-in.** `./st prune` only previews. `./st prune --delete` shows the list, then an interactive `Delete N sessions (X MB)? [y/N]` prompt before touching anything.
- **Hard delete, no backups/undo** (user's standing choice). The preview is the last look.
- **Scope:** `--project <selector>` (repeatable) prunes one repo/dir at a time; omitted = all.

## What gets deleted, per session

1. The transcript file `thread["file"]` (`<uuid>.jsonl`).
2. Its sidecar directory `<uuid>/` in the same folder, if present (holds `subagents/` transcripts and file-history snapshots).
3. The session is removed from `registry["threads"]` and a tombstone is appended (`{sessionId, deletedAt, reason: "prune", harvestPath}`) so every view reflects it immediately without a re-scan.

## Components

### `sessiontracker/prune.py` (pure, unit-tested)
- `prune_candidates(reg, now_iso, *, older_than_days=7, max_size_bytes=50*1024, max_turns=12) -> list[dict]` — threads matching the candidate rule, excluding `decision=="keep"`. Uses `overview.age_days`. No side effects.

### `sessiontracker/registry.py` (add)
- `prune_session(reg, session_id, *, reason="prune") -> dict` — remove the thread from `threads`, append a tombstone (preserving its `harvestPath`); idempotent if already gone (no error, no duplicate tombstone). Raises nothing for a missing id (it may already be tombstoned).

### `bin/st-prune` (CLI, side-effecting)
Flags: `--registry`, `--project` (repeatable), `--older-than` (days, default 7), `--max-size` (KB, default 50), `--max-turns` (default 12), `--delete`.

Flow:
1. Load registry; compute `now` at the edge.
2. `cands = prune_candidates(...)`; apply `--project` via `projects.filter_by_projects`.
3. **Print the preview** — one row per candidate, **oldest first**: `[status] label  (Nd, SIZE, M msgs)  project`, then a summary `N sessions, TOTAL size`.
4. If **not** `--delete`: stop (preview only). Print a hint: "re-run with --delete to remove these."
5. If `--delete`: prompt `Delete N sessions (TOTAL)? [y/N] `. On anything but `y`/`yes`, abort with "aborted, nothing deleted."
6. On confirm, for each candidate: delete the `.jsonl` and the `<uuid>/` sidecar dir (`shutil.rmtree` for the dir, ignore-if-missing). On `OSError`, warn to stderr and skip (leave it in the registry). On success, `registry.prune_session(reg, sid)`.
7. Save the registry. Print `deleted N sessions (TOTAL), skipped M`.

### `./st` dispatcher
Generic passthrough already routes `st prune` → `bin/st-prune`. Add a usage line.

## Edge cases & robustness

- **File already gone** (deleted out-of-band): `Path.unlink(missing_ok=True)` + `rmtree(..., ignore_errors=True)`; still tombstone it.
- **Permission / OSError mid-batch:** warn + skip that session, continue; registry saved once at end reflects only successful deletes.
- **`keep`-protected:** excluded at the candidate stage, never shown in the preview.
- **Empty candidate set:** preview prints "nothing to prune" and exits 0.
- **Tombstone hygiene:** `prune_session` doesn't double-append if the id is already tombstoned.

## Non-goals (this slice)

- Harvesting knowledge to docs, archiving (moving transcripts out), interactive one-by-one triage, and re-classifying mislabeled sessions — all part 2.
- Backups / undo.

## Testing

- **`prune.py`:** `prune_candidates` for each axis — too-recent excluded; small-by-size included; small-by-turns included; large+many-turns excluded; the OR (small size but many turns, or big but few turns) included; `decision="keep"` excluded; `decision="abandon"`/`"harvested"` included; threshold boundaries (exactly `older_than_days`, exactly `max_size`).
- **`registry.prune_session`:** removes the thread, appends one tombstone with `reason="prune"` + preserved `harvestPath`; second call for the same id doesn't duplicate; other threads untouched.
- **`bin/st-prune` (subprocess, temp registry + temp transcript files):** preview writes nothing and deletes nothing; `--delete` with `y` removes the `.jsonl` + sidecar dir and tombstones + drops from threads; `--delete` answered `n` deletes nothing; `--project` scoping; a missing transcript file is tolerated (still tombstoned); a `keep`-marked session never appears.
