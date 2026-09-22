# Inbox TUI

`ringleader/inbox/app.py`: the Textual terminal UI for working the dormant-session queue.

## Purpose

`rl inbox` is the interactive counterpart to `rl triage`. Triage prints a list; the inbox lets
you act on each row without retyping session ids, which is the difference between reviewing five
sessions and reviewing fifty.

Textual is the package's **only runtime dependency** (`textual>=8`).

## How it works

### Three tabs

`_TABS = ("inbox", "selected", "harvesting")`, cycled with `tab` / `shift+tab`:

- **inbox** — undecided live sessions, one row per session (`project: description`).
- **selected** — sessions picked for today (`selectedForDay` in the registry).
- **harvesting** — sessions with an un-integrated digest: harvested but not yet archived,
  shown with a digest preview.

Most actions are inbox-only; the handlers check `_active_tab` and no-op elsewhere.

### Bindings

| Key | Action |
|---|---|
| `ctrl+j` / `ctrl+k` (or `j` / `k`, ↓ / ↑) | move the cursor |
| `tab` / `shift+tab` | cycle tabs (`priority=True`, to beat Screen's default focus-next) |
| `h` | harvest — an empty digest archives immediately, a non-empty one moves the row to **harvesting** |
| `d` | stage a delete, applied on exit |
| `u` | undo the last staged delete; in **selected**, un-select |
| `s` | snooze for a fuzzy duration |
| `K` | keep long-term (**capital** K) |
| `a` | in **harvesting**: mark integrated and archive |
| `enter` | select for today → **selected** |
| `;` | detail view (project, status, whatItIs, last prompt, git branch, condensed transcript) |
| `q` | quit |

`ctrl+j` / `ctrl+k` come from `tui/keys.py:NAV_BINDINGS`, the motions shared by every
ringleader TUI. They are ctrl-modified because a TUI that filters as you type cannot spend bare
`j` and `k` on navigation. The inbox has no text input, so it binds the bare keys as well.

`enter` never fires its binding directly — `ListView` consumes it and posts `Selected`, so
`on_list_view_selected` dispatches per tab. The binding is kept for the footer text.

In the detail view, `h` or `escape` returns to the list.

### Snooze durations

`s` accepts fuzzy input via `fuzzytime.py`: `2h`, `3d`, `2w`, `1mo`, `tomorrow`,
`next monday`, or a bare integer meaning days.

### Stale-state roundup

On entry the app compares the registry's `lastRoundupAt` against `roundup_stale_hours` (config,
default 8). If stale — or never run — it offers to run `rl roundup` first. On **y** the roundup
runs on a background worker and fires a desktop notification when done, then drops you into the
inbox.

## Key entry points

- `commands/inbox.py` — `cmd_inbox()`, the launcher.
- `inbox/app.py` — `_TABS`, `BINDINGS`, the per-tab action handlers.
- `config.roundup_stale_hours` — the staleness threshold.
- `fuzzytime.py` — snooze duration parsing.

## Gotchas

- **`rl inbox --help` lists an abridged key set** (`j/k` … `h/d/s/enter/;/u`). It omits `K`,
  `a`, `q` and the tab cycling. `BINDINGS` is authoritative.
- **Keep is capital `K`.** Lowercase `k` moves the cursor up.
- **Deletes are staged, not immediate** — they apply on exit, which is what makes `u` possible.
  Harvest and archive are not staged.
- **`roundup_stale_hours` is read from the config file but `rl config set` will not write it** —
  the setter accepts only `ai`, `model`, `llm.provider` and `archive.retention_days`. Edit the
  file to change it.
- **Textual owns the terminal**, so diagnostics that would otherwise reach stderr are invisible
  while the app runs.

## Why

A TUI rather than a series of prompts: the work is triage over a queue, where the cost is
context-switching between reading a row and typing a command that references it. Keeping the
selection in the UI removes the id-copying entirely. Staged deletes exist because the one
irreversible action in the loop is a single keypress away from the cursor keys.

## Related

- [features/views-and-triage.md](../features/views-and-triage.md)
- [registry.md](registry.md)
