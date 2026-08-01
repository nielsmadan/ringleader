# project-cleanup — Design Spec (curation, part 2 of 2)

**Date:** 2026-06-23
**Status:** Approved design, pre-implementation
**Depends on:** Phase 1/2 + `st prune` (part 1); the `/doc` skill (`~/ac/claude/skills/doc`).

## One-line

A guided "clean up a project" flow: prune the junk, then walk a repo's remaining dormant sessions one by one — delete, finish (harvest knowledge into the repo's docs + archive the transcript out of `~/.claude/projects`), or leave open.

## Goal

Turn a repo's pile of dormant sessions into: useful knowledge captured in the product's docs, finished transcripts archived out of the resume picker, junk gone, and a small set of genuinely-open threads left to continue — without re-nagging about untouched ones.

## Architecture — three components, built B → A → C

The harvest is **agentic** (integrating knowledge into docs needs LLM judgment), so it lives in skills; the file/registry mechanics are deterministic `st` CLIs. Components:

### A. `/doc --session` — the pure integrator (in the `/doc` skill)
A new mode of the existing doc skill. **Input:** `--md <file>` (a rendered conversation) and optional `--report <file>`. **Behavior:** extract the three knowledge types from the conversation and integrate them into the repo's existing docs *following doc's own conventions* — gotchas → each doc's `## Gotchas`, decision rationale → `## Why`, behavior changes → refresh `## How it works (current state)`; locate targets via `docs/overview.md`; **never touch `docs/product/`** (owned by `review-product`). Preview the proposed edits, write on confirm (uncommitted). **Output contract:** write the list of doc files it touched (newline-separated) to `--report <file>` so the caller can record them. **Fallback** when the repo has no `docs/`: write one `docs/session-harvests/<name>.md` and report that path. A `--session` run knows nothing about `.jsonl` or session-tracker — md in, docs integrated, paths reported.

### B. session-tracker deterministic primitives (this repo, TDD'd CLIs)
- **`st export <id> [--out <path>]`** — look up the session in the registry, render its `.jsonl` to readable conversation markdown (USER/ASSISTANT turns, tool calls noted briefly, metadata records skipped) to `--out` or stdout.
- **`st archive <id>`** — move `<uuid>.jsonl` + `<uuid>/` sidecar to `archive/<encoded-project>/` (repo-local, gitignored); tombstone `reason:"archived"` with `archivePath` + carried-over `harvestPath`; remove from `threads`.
- **`st delete <id>`** — delete the files + tombstone (`reason:"deleted"`); the single-session form of prune, for triage's "delete."
- **`st mark-harvested <id> --paths <p1,p2,…>`** — set `decision="harvested"`, `harvestPath=<paths>`.
- **`st review <id>`** — set `reviewedAtMtime = <current mtime>` (the "open, leave it" marker).
- registry helpers behind these: `mark_harvested`, `archive_session`, `delete_session` (reuse `prune_session` with a reason), `mark_reviewed`.

### C. skills — the agentic orchestration (this repo, `.claude/skills/`)
- **`session-harvest`** (manual one-off, "I remember this session has good info"): `st export <id>` → invoke `/doc --session --md <export> --report <r>` → `st mark-harvested <id> --paths <r contents>`. Harvest only — does **not** archive (you keep it resumable). Reused as the harvest step of `clean-project`.
- **`clean-project`** (the headline flow):
  1. Run `st prune --project <X>` (its own preview → y/N).
  2. Build the triage list: sessions in repo `X` still in `threads`, **excluding** `decision=="harvested"` and any whose `reviewedAtMtime == mtime` (reviewed and untouched since).
  3. For each (oldest first), read it, present a short summary, and ask **delete / finished / open**:
     - **delete** → `st delete <id>`
     - **finished** → `session-harvest` steps (export → `/doc --session` → mark-harvested) **then** `st archive <id>`
     - **open** → `st review <id>`
  4. Report a tally at the end.

## Data model / registry changes

- New thread field **`reviewedAtMtime`** (default `null`); **preserved by `reconcile`** (same treatment as `decision`/`appliedName` — never overwritten by metadata refresh). New threads default it `null`.
- `decision="harvested"` already drives `effective_status` → `harvested` (not live), so harvested sessions drop out of overviews and the triage list.
- Tombstone gains `reason:"archived"` (with `archivePath`) and `reason:"deleted"` alongside the existing `"prune"`/`"gc"`. Archived/deleted leave `threads`; `reconcile` won't re-add them (file gone from `projects/`, already tombstoned).
- `archive/` and any export temp files are gitignored.

## Seam contracts

- **C → B:** plain CLI calls (`st export/archive/delete/mark-harvested/review`) via bash.
- **C/`session-harvest` → A:** the agent invokes the `/doc` skill (Skill tool) with `--md` (the export) and `--report` (a temp path); after it returns, reads the report file for the touched doc paths and passes them to `st mark-harvested`.
- **A → anything:** none. `/doc --session` is a leaf — md in, docs out, report written.

## Edge cases & robustness

- **Session whose checkout/repo is gone:** `st export` still works (reads the registry's stored file path under `~/.claude/projects`); harvest target repo may be absent → `/doc --session` reports "no repo/docs," and the user can still choose archive/delete.
- **Harvest finds nothing worth documenting:** `/doc --session` reports an empty touched-list; `mark-harvested` records `harvestPath=null`; archive still proceeds.
- **Archive name collision** (same `<uuid>` already archived): shouldn't happen (uuids unique); if the dest exists, error out rather than overwrite.
- **Large transcripts:** `st export` renders the full conversation; `/doc --session` is responsible for handling size (chunk/sample). Flag for A's implementation; not solved in B.
- **Re-runs:** harvested → excluded; archived/deleted → gone; open-unchanged → skipped via `reviewedAtMtime`; open-then-worked-on → `mtime` advanced, reappears.
- All deletes/moves: tolerate already-missing files (`missing_ok`, `ignore_errors`), warn + continue.

## Testing

- **B (code) — TDD:** `export.render_conversation` (pure: turns rendered, metadata skipped, tool calls noted, empty/garbage tolerated); registry helpers (`mark_harvested` sets decision+paths; `archive_session` removes thread + tombstones `archived` with `archivePath`/`harvestPath`; `mark_reviewed` sets field; `reconcile` preserves `reviewedAtMtime`); CLI subprocess tests (`st export` renders to file; `st archive` moves `.jsonl`+sidecar into `archive/` and tombstones; `st delete` removes + tombstones; `st review` sets the marker; missing-file tolerated).
- **A (`/doc --session`, skill prose) — verification:** run it on a sample rendered conversation against a sample repo that has `docs/` with `## Gotchas`/`## Why` sections; confirm the right sections gained the right nuggets, `docs/product/` untouched, report file lists the edited paths; and the no-`docs/` fallback writes `docs/session-harvests/<name>.md`.
- **C (skills prose) — verification:** dry-run the `clean-project` flow against a throwaway registry + copied transcripts; confirm prune runs first, the triage list excludes harvested/reviewed-unchanged, and each outcome calls the right primitives; confirm `session-harvest` standalone does export→doc→mark without archiving.

## Non-goals

- `/clear`-boundary splitting; Todoist linking; auto-committing the doc edits or the archive (user commits docs; archive is gitignored).
- Re-classification accuracy improvements beyond what triage's human-in-the-loop already provides.
- Putting `st` on PATH (still repo-local).
