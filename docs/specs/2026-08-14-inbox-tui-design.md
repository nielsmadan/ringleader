# Inbox TUI — Design

**Goal:** An inbox-zero terminal UI, `rl inbox`, for blitzing through dormant sessions —
navigate with `j`/`k`, and per session harvest / delete / snooze / select-for-today —
plus `rl roundup`, a combined refresh + prune + enrich verb the inbox runs on entry when
things are stale.

**Architecture:** A [Textual](https://textual.textualize.io/) app under
`ringleader/inbox/`, launched by `rl inbox`, importing the registry/command internals
**in-process** (no subprocess). Textual is an optional `ringleader[tui]` extra so the core
CLI stays dependency-light. The app is tested headlessly via Textual's `Pilot` harness.
`rl roundup` is a plain CLI verb reused by the inbox's pre-flight.

**Tech Stack:** Python ≥3.13; Textual (optional `[tui]` extra); the existing registry /
`refresh` / `prune` / `enrich` / `harvest` / `complete` / `snooze` internals; `pytest`
(+ Textual `Pilot`), `ruff`.

## Scope

Spec 3, first version — the third pillar (after the CLI and the LLM capability). In scope:
`rl roundup`, `rl inbox` with three tabs (inbox / selected / harvesting), the keybindings
below, a detail view, a fuzzy-duration parser for snooze, a desktop notification on
roundup completion, and the small registry/config additions those need. **Out of scope
(future):** starting selected sessions, bulk actions on the harvest queue, and any
repo/memory-layer integration of harvests.

## Decisions

- **In-process Textual app**, optional `[tui]` extra. Tested via `Pilot`.
- **`rl roundup`** = refresh → prune (deletes tiny+stale) → enrich, recording
  `lastRoundupAt`. The inbox pre-flight offers it when stale; it is also a standalone verb.
- **`roundup`'s prune deletes** the tiny+stale junk (the "garbage collection") — gated
  behind the user's explicit *y* at the pre-flight prompt (or an explicit `rl roundup`
  run), with the existing conservative thresholds; it reports the count.
- **`selectedForDay`** — a new date-stamped registry field; `enter` sets it to today.
- **Harvesting-pending is derived, not a new field:** `decision=="harvested"` **and not
  archived**. The harvesting tab lists those with their digests.
- **Conditional archive on harvest:** if a harvest yields **nothing durable** (empty
  summary + no gotchas/decisions/whatChanged), the session is archived immediately;
  otherwise it stays live in the harvesting tab, pending integration.
- **Delete is staged with undo**, applied on exit with a printed summary (no interactive
  confirm — the staging + undo is the fat-finger safety) — the one destructive key is
  fat-finger-safe in a fast `j`/`k` flow. Harvest/snooze/select apply immediately
  (snooze and select are reversible; harvest runs live).

## Components

### 1. `rl roundup [--yes] [--registry <path>]` (`ringleader/commands/roundup.py`)

Runs, in order: **refresh** (scan sources, reconcile — reuse `refresh.auto_refresh` /
`scanner.active_sources`), **prune** (delete tiny+stale — reuse `prune.prune_candidates`
+ the delete path, actually deleting), **enrich** (LLM-classify the newly-pending — reuse
`enrich`'s flow). Then set `lastRoundupAt = now` in the registry and save. Prints a
one-line summary (`refreshed N, pruned M, enriched K`). `--yes` skips any confirm (the
inbox passes it after its own prompt). If no model is configured, the enrich step is a
no-op with the standard guidance (refresh + prune still run). Reuses existing functions —
no reimplementation of scan/prune/enrich logic.

`roundup_stale_hours` is added to `Config` (default `8`); `lastRoundupAt` (ISO) is stored
in the registry alongside `lastScan`.

### 2. `rl inbox` (`ringleader/commands/inbox.py` → launches `ringleader/inbox/app.py`)

`cmd_inbox` checks the `[tui]` extra is installed (clear message + non-zero if Textual is
missing), then runs the Textual app. The app:

**Pre-flight.** On mount, if `now - lastRoundupAt > roundup_stale_hours` (or it's never
run), show a modal: *"Sessions may be stale — refresh, prune & enrich now? [y/n]"*.
- **y** → run `roundup` on a Textual **worker** (async, non-blocking) behind a progress
  screen (steps + enrich per-session progress); on completion fire a **desktop
  notification** (§5) and dismiss to the inbox.
- **n** / fresh → straight to the inbox.

**Three tabs** (`tab` / `shift+tab` cycle inbox → selected → harvesting):

- **inbox** — undecided live sessions (the triage inbox: not reviewed/harvested/snoozed,
  and `selectedForDay != today`). Rows render `projectName: <short description>`
  (description = `whatItIs` or the ai/title). `j`/`k` move the cursor.
  - `h` — **harvest** (Textual worker, async): the row shows a spinner; run the distill.
    Empty digest → archive the session (leaves the list). Non-empty → the session moves to
    the *harvesting* tab. Reuses `harvest` + the conditional-archive logic. No model
    configured → a toast telling the user to `rl config`.
  - `d` — **stage delete**: the row is removed from the inbox and queued; `u` un-stages the
    most recent. Actual `registry` + file deletion is applied **on exit**, printing a
    *"deleting N session(s)"* summary — no interactive confirm (the staging + `u` undo
    already provides the fat-finger safety).
  - `s` — **snooze**: a prompt input; the user types a fuzzy duration (§4); on submit the
    session is snoozed (`snooze_thread` with the parsed `until`) and leaves the list.
  - `enter` — **select for the day**: set `selectedForDay = today`, move to the *selected*
    tab.
  - `;` — **detail view**: a full-screen panel with project, status, `whatItIs`, last
    prompt, git branch, and the condensed transcript (`export.render_conversation`); `h`
    returns to the list.
- **selected** — sessions with `selectedForDay == today`. v1: view + `u`/`enter` to
  un-select (back to inbox). (Future: "start these".)
- **harvesting** — sessions with an un-integrated digest (`decision=="harvested"`, not
  archived). Shows each session + its digest (from the harvest store); an action `a`
  (*mark integrated & archive*) archives it once the user has folded the digest in.

### 3. Data model (`ringleader/registry.py`)

- Add `selectedForDay: str | None` (a `YYYY-MM-DD`) to the thread record + a
  `select_for_day(reg, session_id, day)` mutator; `enter` sets it, un-select clears it.
- `lastRoundupAt` persisted at the registry top level.
- No new field for harvesting-pending — derived from `decision`/archive state.

### 4. Fuzzy duration parser (`ringleader/fuzzytime.py`)

`parse_duration(text, now) -> str (ISO until)` handling: `2h`/`3d`/`2w`/`1mo`;
`tomorrow`, `next week`, `next monday`, `monday`; a plain integer = days. Invalid input →
a clear error the snooze prompt surfaces (no snooze applied). Reuses `snooze`'s `--until`
path. Pure function, unit-tested independently of the TUI.

### 5. Notification (`ringleader/notify.py`)

`notify(title, message)` — macOS `osascript -e 'display notification …'` (no dependency);
a no-op (best-effort) on other platforms or if `osascript` is absent. Fired when a
worker-run `roundup` completes, so the user can look away during the wait.

## Error handling / degradation

- `[tui]` extra missing → `rl inbox` prints "install ringleader[tui]" and exits non-zero.
- No model configured → roundup's enrich and inbox `h` degrade with the standard guidance
  (the rest still works).
- A per-session harvest error → a toast; the session stays in the inbox.
- Empty inbox → a friendly "inbox zero" state.

## Testing

- **`Pilot`-driven app tests:** key presses drive cursor movement, tab switching, and each
  action's state transition (harvest → moves/archives, delete → staged + `u` undo +
  applied-on-exit, snooze → leaves + `until` set, select → selected tab). The Model seam is
  **mocked** (fake digests, incl. an empty one for the conditional-archive path); a tmp
  registry + `XDG_DATA_HOME` — **never a real model or real session data**.
- **`roundup`:** refresh + prune + enrich run in order against a tmp registry with a fake
  model; `lastRoundupAt` set; pre-flight staleness threshold honored.
- **`fuzzytime.parse_duration`:** table-driven unit tests (each format → expected ISO;
  invalid → error). **`notify`:** `osascript` invocation mocked.

## Out of scope

- Starting selected sessions; bulk harvest-queue actions; repo/memory integration of
  digests (the harvesting tab is view + archive only).
- Non-macOS notification backends beyond the best-effort no-op.
- Config UI for keybindings; theming.
