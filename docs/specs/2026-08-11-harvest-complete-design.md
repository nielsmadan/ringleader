# Harvest + Complete — Design

**Goal:** Give ringleader a tool-native way to capture a dormant session's durable
knowledge — `rl session harvest` distills a session (via the C1 Model seam) into a stored
markdown **harvest**, `rl session complete` = harvest + archive, and `rl harvests` lists the
store. Integration of harvests into repo docs is left to the skills layer / the user.

**Architecture:** `harvest` reuses the C1 `Model` seam (`complete_json` + a schema): it
feeds the model a **condensed** render of the session (tool output stripped, ~10× smaller
than the raw transcript), gets back a structured distillation, renders it to markdown, and
writes it to a central harvest store under `state_dir()/harvests/` as a frontmatter file
tagged with the session's repo. `complete` chains `harvest` then `archive`. No integration
into repo docs — ringleader distills and stores; placing harvests is the skills layer's job.

**Tech Stack:** Python ≥3.13, stdlib; the C1 Model seam (`ringleader/models/`); `pytest`,
`ruff`. No new runtime dependencies (the API backend's `llm` extra already exists).

## Scope

Spec C2 — the second half of the LLM capability, on top of C1 (Model seam + `rl config` +
`enrich`). In scope: the condensed render, `rl session harvest`, the harvest store,
`rl harvests`, and `rl session complete` (implementing the reserved `session complete`).
Out of scope: intelligent integration of harvests into repo docs (skills layer / user), the
TUI (Spec 3), any target hook (`harvest.command`/`harvest.skill`) — ringleader stores, the
skills integrate.

## Naming

The verb is **`harvest`** — it means the valuable *yield* (gather the crop worth keeping,
leave the chaff), reads cleanly as verb and noun (`rl session harvest` → `rl harvests`), and
matches the vocabulary already in the codebase (`mark harvested`, `harvestPath`) and the
skills layer, so the whole tool speaks one language. The combined "I'm done with this" verb
is the neutral **`complete`** (= harvest + archive) — deliberately *not* a second metaphor
(`retire`) on top of `harvest`. Plain `archive` stays for archive-without-harvest.

## Decisions

- **Central store, not the repo.** Harvests live under `state_dir()/harvests/` — one
  markdown file per harvest, with YAML frontmatter. ringleader never edits a repo's docs.
- **Each harvest records:** `folderPath` (the session's local repo/cwd), `repoUrl` (git
  remote — groups harvests by repo regardless of local path), plus `sessionId`, `name`,
  `harvestedAt`; the distilled markdown is the file body (the **content**).
- **Condensed input by default.** Distilling the raw transcript is dominated by tool
  output; `harvest` feeds the model a condensed render (user/assistant messages + tool-call
  headers; tool-result bodies dropped/capped) — the better and ~10× cheaper input. Full
  (raw) render is a non-default option (`--full`).
- **Backend-agnostic.** `harvest` only ever asks the model for text (structured JSON), so it
  works with every C1 backend (claude/codex agent CLIs and the `llm` API backend) — no file
  access needed by the model.
- **No repo-doc integration in C2.** `rl harvests` is the inbox; the skills layer (or the
  user) reads it and integrates. This deliberately excludes the `/doc`-style merge.

## Components

### 1. Condensed session render (`ringleader/transcript.py` or a new `ringleader/condense.py`)

A renderer that turns a session transcript into condensed markdown for model input:
- Keep: user prompts and assistant messages (text + reasoning), each tool **call** as a
  one-line header (`tool: <name> <short args>`).
- Drop/cap: tool **result** bodies — replace with a short marker (e.g. first ~200 chars
  then `[…N lines omitted…]`), or omit entirely for large results.
- Bound the whole thing to a max size (keep head + tail if a session is enormous).

The existing `rl data export` renders the *full* session; `harvest` uses this condensed
render instead. (Reuse the transcript-parsing already behind `export`; add the condensed
rendering mode.)

### 2. `rl session harvest <id> [--dry-run] [--full] [--registry <path>]` (`ringleader/commands/harvest.py`)

1. Resolve `model = active_model(load_config())`; `None` → print the enrich-style guidance
   message and return 0 (no-op).
2. Load the session's transcript; produce the **condensed** render (`--full` → raw).
3. `model.complete_json(prompt, HARVEST_SCHEMA)` → structured distillation; render to
   markdown.
4. Write the harvest file to `state_dir()/harvests/<slug>.md` with frontmatter
   (`sessionId`, `name`, `folderPath`, `repoUrl`, `harvestedAt`) and the markdown body.
   `--dry-run` prints instead of writing.
5. Set the thread's harvested flag (reuse the registry's mark-harvested path, recording the
   harvest file path) so the skills layer's bookkeeping is consistent; save the registry.
6. Print a summary (`harvested <id> -> <path>`).

`HARVEST_SCHEMA` (module constant):
```python
HARVEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "gotchas": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "whatChanged": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
}
```
Rendered to markdown with a title + `## Summary / ## Gotchas / ## Decisions / ## What
changed` sections (empty sections omitted).

### 3. Harvest store + `rl harvests [--json]` (`ringleader/harvests.py` + `ringleader/commands/harvests.py`)

- `ringleader/harvests.py`: `harvests_dir()` (`state_dir()/harvests`), `write_harvest(meta,
  markdown) -> Path`, `load_harvests() -> list[Harvest]` (parse each file's frontmatter +
  body). `Harvest` dataclass: `path`, `folder_path`, `repo_url`, `session_id`, `name`,
  `harvested_at`, `content`.
- `rl harvests` (`commands/harvests.py`): list every stored harvest. Text mode: one row per
  harvest with **folder path**, **repo url**, and a short content preview. `--json`: the
  full records incl. content. Sort newest first; group/annotate by repo where useful.

### 4. `rl session complete <id> [--registry <path>]` (`ringleader/commands/complete.py`)

Chains harvest → archive:
1. Run the harvest flow (steps 1-6 above). If harvest is a no-op (no model) or raises, print
   the reason and **do not archive** — `complete` always means genuinely harvested-and-archived.
2. On a successful harvest, invoke the existing archive path (`commands/archive.py`'s core /
   `registry.archive_session`) to move the transcript to `archive/` and record the tombstone.
3. Print `completed <id> (harvest -> <path>, archived -> <dest>)`.

Implements the reserved `session complete` stub: wire `_SESSION["complete"] =
complete.cmd_complete` in the dispatch TABLE, drop `session complete` from `cli.py`
`_RESERVED`, and update the README/AGENTS/`test_rl_dispatch` accordingly (it was
"coming soon").

## Error handling / degradation

- No model available → `harvest`/`complete` are no-ops with the "configure with `rl config`"
  guidance; `complete` archives nothing.
- `ModelError` (bad/empty model output, backend failure) → reported; harvest not written;
  `complete` archives nothing.
- A session already harvested → `harvest` overwrites its harvest file (idempotent);
  `complete` re-harvests then archives (default: re-harvest for freshness; a skip-if-exists
  option is a plan-time detail).

## Testing

- **No real model calls, ever** — mock the `Model` seam (a fake returning a canned
  `HARVEST_SCHEMA` object); tmp `state_dir()`/harvest store (via `XDG_DATA_HOME`) and tmp
  `--registry`; never scan real data.
- **Condensed render:** a transcript with big tool outputs renders with the tool-result
  bodies stripped/capped and the user/assistant messages retained; bounded to the max size.
- **harvest:** writes a frontmatter file with `folderPath`/`repoUrl`/`sessionId` and the
  rendered markdown body; sets the harvested flag; `--dry-run` writes nothing.
- **harvests list:** `rl harvests` shows folder path + repo url + preview; `--json` includes
  full content; empty store handled.
- **complete:** harvest-then-archive on success; no-model / `ModelError` → nothing archived
  (session stays live).
- **Graceful degradation:** no model → no-op + guidance (exit 0).

## Out of scope

- Integrating harvests into repo docs (the skills layer / the user's own tooling reads
  `rl harvests`).
- The TUI (Spec 3).
- A target hook (`harvest.command`/`harvest.skill`) — dropped; ringleader stores, skills integrate.
- Renaming `archive` (kept as-is) or broader circus renames.
