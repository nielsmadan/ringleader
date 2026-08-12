# Codex Session Source — Design

**Goal:** Add Codex as a second session `Source` to prove the `sources/` seam
generalizes across harnesses — Codex sessions flow through the same
`discover()`/`parse()` → `NormalizedSession` → registry → `overview`/`triage`/`prune`
pipeline as Claude Code, and the generic write-ops work on them.

**Architecture:** The session `Source` seam already normalizes Claude Code transcripts
into `NormalizedSession`; this spec adds a second implementation (`CodexSource`) reading
`~/.codex/sessions/`, wires the scanner to run every auto-detected source, and resolves
the Claude-specific assumptions that surface downstream. It introduces a per-source
**capability** notion so harness-specific operations (naming writeback) gate cleanly
instead of special-casing.

**Tech Stack:** Python ≥3.13, stdlib only (JSONL parsing), `pytest`, `ruff`. No new
runtime dependencies.

## Scope

This is a **validation spec** — its purpose is to exercise and harden the `Source` seam,
not to reach Codex feature parity. It is Spec A of a two-part effort:

- **Spec A (this doc):** Codex **session** source — read path + generic write-ops +
  source-visible views + the seam-leak fixes that surface.
- **Spec B (later):** Codex **memory** source — a separate `MemorySource` seam reading
  Codex's `memories_1.sqlite`. Independent of this spec (different protocol, format, and
  registry section); deferred so the session seam is proven first.

Crossover between A and B is a single shared idea — "a harness has an identity and a
root on disk." Spec A must keep source registration general enough that a memory source
slots in later; it must not hardcode "sources are session sources."

## Decisions

- **Auto-detect sources.** A source is scanned iff its root exists on disk
  (`~/.claude/projects`, `~/.codex/sessions`). No config — adding a harness means
  registering a `Source`. (Config-based opt-in is a Spec 2 concern and deliberately
  avoided here.)
- **Registry identity is `(source, session_id)`.** UUID collisions across harnesses are
  unrealistic, but `source` is persisted and carried through reconcile/lookup so no code
  path assumes a single harness.
- **Sources declare capabilities.** A small declared set per source (e.g.
  `applies_names`) lets commands gate per-harness. The session *name* is always a
  registry field shown in every view; only *writing it back into the harness's own
  resume picker* is a capability.
- **Source is visible + filterable** in the per-session views (a `source` badge/column;
  a `--source <name>` filter).
- **One accepted behavior change:** `rl session archive` destination folders move from
  the transcript's parent-dir name to the project label (see §6). This changes where
  Claude archives land; archives are ringleader-managed, so this is acceptable.

## Codex on-disk format (reference)

- Layout: `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<session_id>.jsonl`
  (date-partitioned; ~467 sessions on the reference machine).
- Line 1 is `{"type":"session_meta","payload":{...}}` with `session_id`, `cwd`,
  `timestamp`, `model_provider`, `cli_version`.
- Subsequent lines: `response_item` (messages, function calls), `event_msg` (tool
  events, turn summaries), `turn_context`, `world_state`, `compacted`, etc.
- **User messages:** `response_item` payloads with `type == "message"` and
  `role == "user"`; text lives in `payload.content[].text`.
- **cwd is inside the file** (`session_meta.payload.cwd`), unlike Claude which encodes it
  in the directory name — so Codex `discover()` cannot know the project without a
  `parse()`.
- **The first user message is an injected `AGENTS.md` preamble** (analogous to Claude's
  injected context) — the parser must skip it to find the real first/last prompt.
- Native names are append-only entries in `~/.codex/session_index.jsonl` with `id`,
  `thread_name`, and `updated_at`; the latest entry for an id wins. Summary and ai-title
  still normalize to `None`.

## Components

### 1. `ringleader/sources/codex.py` — `CodexSource`

Implements the existing `Source` protocol (`sources/base.py`).

- `name = "codex"`, `root = ~/.codex/sessions` (overridable via env for tests, mirroring
  `SESSION_TRACKER_PROJECTS_DIR`).
- `discover() -> Iterator[RawRef]`: glob `root/**/*.jsonl`, yield
  `RawRef(source="codex", file=str(p), mtime=_iso_mtime(p), size_bytes=st.st_size)`.
  Stat only — cheap, no parse.
- `parse(ref) -> NormalizedSession`:
  - read line 1 `session_meta` → `session_id`, `cwd`, timestamp, `model_provider`.
  - iterate `response_item`/`message`/`role=="user"` payloads → `message_count` and
    `last_prompt` (skipping the injected `AGENTS.md` preamble); collect recent turns into
    `tail_text` (bounded, mirroring Claude's `tail_turns`).
  - derive git fields from `cwd` via the existing `gitinfo` module (source-neutral).
  - `custom_title` from the latest matching session-index entry; `summary=None`,
    `ai_title=None`.
  - return `NormalizedSession(source="codex", session_id=…, cwd=…, mtime=ref.mtime,
    size_bytes=ref.size_bytes, last_prompt=…, file=ref.file, message_count=…,
    tail_text=…, git_*=…)`.

### 2. Multi-source wiring

- A source registry lists the source classes with their roots + capabilities:
  `SOURCES = (ClaudeCodeSource, CodexSource)`.
- `active_sources() -> list[Source]`: instantiate each source whose root exists.
- `ringleader/scanner.py` already accepts `sources=[...]`; its callers
  (`sync.cmd_scan`) pass `active_sources()` instead of a hardcoded
  `[ClaudeCodeSource(...)]`.

### 3. Identity & registry (`ringleader/registry.py`)

- `reconcile` matches existing threads by `(source, session_id)` and stores `source` on
  each thread record (persist it in the schema).
- A `source_for(thread) -> Source` helper maps a thread's `source` string to its `Source`
  instance (from `SOURCES`) — used by capability checks and file operations.

### 4. Capabilities

- `Source` gains `capabilities: frozenset[str]` (default empty). Both
  `ClaudeCodeSource` and `CodexSource` declare `frozenset({"applies_names"})`.
- `rl session name` (`commands/name.py`) filters to threads whose `source_for(...)`
  has `applies_names`; it reports the count of sessions skipped for lacking the
  capability rather than erroring. Names remain visible in `overview`/`triage` for all
  sources (they come from classification, not from the writeback).

### 5. Views: source badge + filter

- `overview` (per-session view, `-s`) and `triage` render a `source` column/badge
  (`claude` / `codex`).
- `overview` and `triage` accept `--source <name>` (repeatable) to filter threads by
  source. The rollup notes the source mix where a project has sessions from more than one
  harness.

### 6. Seam-leak fixes

- **`commands/archive.py` destination.** Today: `dest_dir = archive_root / src.parent.name`
  — the encoded cwd for Claude, but a day-number (`28`) for Codex. Change to a
  source-neutral key: `dest_dir = archive_root / <slug>`, where `<slug>` is the thread's
  `gitProjectLabel` (falling back to a slug of `cwd`, then the source name). Behavior
  change for Claude archives is accepted (§Decisions).
- **Audit pass.** Grep the command layer and `registry.py`/`overview.py` for
  `~/.claude`, `.claude`, `projects/`, `.jsonl` path assumptions and Claude-specific
  record shapes; for each, either generalize through `NormalizedSession`/`source_for`, or
  document it as Claude-only in the code and the spec. Findings recorded in the plan.

### 7. Prune (`commands/prune.py`)

- Prune's tiny+stale logic runs on registry metadata. Codex rollout files bundle tool
  events, so byte-`size_bytes` overstates conversation length — the "tiny" test must use
  `message_count` (source-neutral) rather than bytes, so Codex sessions aren't
  mis-pruned. Stale (mtime age) is already source-neutral.

## Testing

- **Codex source unit tests** (`tests/test_sources_codex.py`): `discover()` finds
  rollout files and skips non-jsonl; `parse()` extracts `session_id`/`cwd`/`last_prompt`/
  `message_count` from a fixture rollout, skips the `AGENTS.md` preamble, and returns
  `source == "codex"` with `custom_title`/`summary`/`ai_title` `None`.
- **Multi-source scan**: `active_sources()` returns both when both roots exist, only the
  present ones otherwise; a scan over a tmp `~/.claude` + `~/.codex` yields threads from
  both with correct `source`.
- **Registry identity**: two threads with the same `session_id` but different `source`
  are distinct records; reconcile updates the right one.
- **Capabilities**: `rl session name` writes Claude titles to their transcripts and Codex
  titles to `session_index.jsonl`; both show their native name in `overview`.
- **Views**: per-session `overview`/`triage` output includes the source badge;
  `--source codex` filters to Codex threads only.
- **Archive**: archiving a Codex session lands under the project-label folder (not a
  day-number) and records the tombstone correctly.
- **Prune**: a Codex session with few messages but large byte-size is treated by
  `message_count`, not bytes.

## Out of scope

- **Codex memory** (`memories_1.sqlite`) — Spec B.
- **Any configuration** — sources are auto-detected only.
- **LLM enrichment / TUI** — later specs.
