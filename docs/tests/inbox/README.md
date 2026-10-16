# Inbox responsiveness

Run this when changing the home TUI's rendering, its row building, the writer or the scan path.
From the repository root after `uv sync`:

```sh
uv run python scripts/benchmark_inbox.py
```

[The script](../../../scripts/benchmark_inbox.py) builds a synthetic registry in a temp directory
with 2,000 threads (`--threads`) and real transcript files, redirects every agent store and
`XDG_*` directory into it, and never touches live data. JSON goes to stdout; scan warnings go to
stderr. The budget verdict is `within_budget` in the JSON; the exit status is 0 either way.

## Phases

- **idle** — a headless 120×40 `InboxApp` whose refresher is gated, so no scan runs. It times
  first paint, then 20 `down` presses on Projects, 20 on the largest project's triage list, and
  10 `g` (keep going) presses on triage. Each `g` is a real writer op against the registry file.
- **under load** — the real refresher, `ctrl+r`, and a `PruneOp` over the fixture's prune
  candidates, both running while 20 `down` presses are timed on Projects. A fresh scan is
  started whenever the app goes quiet, so a scan or the writer is busy for every sample
  (`load_extra_rescans` counts the restarts).
- **cold** — the real `rl inbox --registry <fixture>` in a pty (`tests/tui_e2e.py`), from launch
  to the first `Next:` on screen: interpreter start, imports, Textual, first paint.

## What is measured

A key is timed from posting it to the app until its effect is on screen: the highlight moved, or
the decided row left the list, then one frame (`pause(0)`). `pilot.press` is not used, because it
waits for whole-process CPU idle and so times a concurrent scan instead of the key. First paint
is app start to the counts line rendered.

Not measured: the terminal emulator's own painting, the live registry's real shape (the fixture
is synthetic), and cold start against a budget. Cold start is recorded only.

## Budgets

- First paint < 1 s.
- Key median < 100 ms, idle (Projects and triage `down`) and under load (Projects `down`).

p95 and max are recorded for reading, not budgeted. Compare runs on the same machine, interpreter
and Textual version. A missed budget is fixed at its cause before a run is recorded.

Runs:

- [2026-10-16](runs/2026-10-16.md)
