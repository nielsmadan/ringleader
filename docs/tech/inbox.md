# Inbox TUI

`ringleader/home/` and `ringleader/commands/inbox.py`: the Textual app behind `rl inbox` and bare
`rl` — the daily home for dormant sessions.

## Purpose

Show what is waiting, say what to do next, and let you do it without leaving the terminal:
triage a project, resume a session, run the roundup, prune, read harvests, check usage. Fast,
mechanical decisions are native; naming without a model, harvesting into docs and publishing
harvests are handed to an agent's skills by exiting the app and exec'ing the agent.

Textual is the package's only runtime dependency (`textual>=8`).

## How it works

### Package layout

| Module | Role |
|---|---|
| `home/app.py` | `InboxApp`: the composition root — wires the writer, scans, jobs and roundup driver; navigation, state adoption, the leave barrier; every action a screen can take |
| `home/roundup_run.py` | `RoundupDriver`: the roundup run's state machine (phases → lock → enrich → stamp), the enrich job and its messages |
| `home/jobs.py` | `JobBook`: harvest jobs and side jobs (`run_job`) in flight; `harvest_job` and its result and message types |
| `home/prints.py` | `PrintCoalescer`: stderr print capture coalesced into toasts |
| `home/base.py` | `InboxScreen`, `AppState`, `ScreenTarget`, `RoundupRun` |
| `home/state.py` | pure: `build_home_state`, `next_steps`, `project_queue`, `scope_rows` |
| `home/writer.py` | `Writer`: the single mutation queue |
| `home/overlay.py` | pure: the overlay of identities hidden from the view |
| `home/scan.py` | `ScanService`: at most one scan thread |
| `home/policy.py` | `DELETE_GRACE_SECONDS`, `CONFIRM_EACH_DELETE`, `CONFIRM_BULK`, `SCAN_FRESH_SECONDS` |
| `home/intents.py`, `home/launch.py` | exit intents and the pure handoff builders, and running one after the app has exited |
| `home/palette.py` | the command palette provider and its verb allowlist |
| `home/screens/` | `home`, `triage`, `projects`, `roundup`, `prune`, `harvests`, `detail`, `output`, `usage` |
| `tui/` | shared: `background` (daemon jobs), `modals`, `rows`, `keys`, `runner`, `snapshot` |

`commands/inbox.py::cmd_inbox` builds the app and runs it. After `run()` returns it aborts
background work, joins the writer, kills live model children, returns a crash's non-zero code, and
only then runs the exit intent through `launch.run_intent`.

### Layering

`home/` depends on `decisions`, `roundup`, `enrichment` and `revalidate`; `decisions` and
`roundup` depend on `commands/*` for shared logic (`archive_thread`, `sweep.due`, harvest saving),
and the Prune screen imports `commands.sweep` directly for sweep eligibility. Because
`commands/__init__.py` imports every command module, a library module that needs a command
module imports it as a module (`from .commands import sweep as sweep_cmd`), and a command module
imports a library module as a module (`from .. import roundup as roundup_api`), never
`from ..roundup import name`. `revalidate.py` imports no command module.

### Snapshots, generations and the overlay

The app holds an `AppState`: a registry `Snapshot` with a `generation` (the writer's count of
applied writes), the overlay, live-session ids from presence, pending harvests and model labels.
A scan carries the count read when it started; the writer's post-op reload carries the exact
count. A snapshot is adopted only when its generation is at least the adopted one. A dropped older
scan makes the app submit a `ReloadOp` to pick up what that scan persisted.

The view is the adopted snapshot minus the overlay: identities with a decision queued, running,
or applied at a generation newer than the adopted snapshot. Rows are re-derived from the view and
keyed by `qualified_id`, never by index, so a decided row cannot return from a scan or from
another decision's reload.

### The writer and its ops

`Writer` is one daemon thread draining a queue, one op at a time. Ops are `ArchiveOp`,
`DeleteOp`, `KeepGoingOp`, `SnoozeOp`, `KeepOp`, `HarvestSaveOp`, `PruneOp`, `RoundupPhasesOp`,
`ClassifyOp`, `StampRoundupOp` and `ReloadOp` (`decisions.py`). Screens call `InboxApp` methods
that build ops and `submit` them; they never write.

- Deletes carry a `not_before` of now plus `DELETE_GRACE_SECONDS` (10 s). Everything else runs
  at once.
- `u` calls `undo_last`: the newest queued batch that is not a reload, classify or stamp, going
  further back on repeated presses. An op that has started cannot be undone; an archive is
  reversed with `rl session unarchive`. When part of the batch already ran, the toast says so:
  `undone: 2 of delete 3 sessions (1 already applied)`.
- `cancel(ticket)` drops one queued op; `discard_pending` drops all that have not started.
- In read-only mode every submit except a reload is refused with `read-only: <what> is disabled`.
- An unexpected exception in an op becomes an `internal error: <type>: <message>` outcome and the
  traceback is appended to `<state dir>/inbox-errors.log`. `InboxApp(strict=True)` re-raises it
  instead.
- A registry write that fails after a file already moved reports it: "on disk, but the registry
  was not updated".

### Revalidation

Before every destructive op (archive, harvest-and-archive, delete, prune, sweep) the writer
reloads the thread under the lock the CLI command takes and refuses when the thread is gone, its
registry `mtime` differs from what the user saw, or the transcript's `(size, mtime_ns)` differs
from its `fileFingerprint`. A delete or prune also refuses a thread with no `fileFingerprint`
(`changed since you looked: no fingerprint, rescan`), and `scanner.filesystem_path` refuses to
delete a transcript, or a companion, that is not inside its source's session store. A sweep also refuses a transcript newer than `sweep.GRACE_SECONDS`.
Presence is read fresh: a running session is refused with a pointer to `ringleader-finish`, and a
presence result that cannot tell refuses with `can't tell whether <label> is running`. A refused
op returns its row, shows the reason in a warning toast and asks for a rescan. A harvest is also
revalidated when the session is already harvested.

### The leave barrier

`q`, choosing a resume and every handoff call one `_leave` path. If the writer is busy or a
harvest job is running, it flushes the delete grace, shows `finishing N change(s)…` and keeps the
UI live; the app exits when the writer reports drained. The enrich job is cancelled at once; the
batches it already applied are queued, the rest stays pending. A second `q` while leaving opens
the discard confirm: `y` drops ops that have not started, cancels harvest jobs and asks a long op
to stop at its next item boundary. The item in progress always finishes. The app does not exit
while an enrich job is still draining.

### Scans

`ScanService` runs at most one scan thread. `ctrl+r` during a scan queues one rerun. A scan runs
`refresh.auto_refresh` (with `persist=False` in read-only mode), reads presence and resolves model
labels, then posts `ScanLanded`. The hosted Resume picker and the Roundup screen wait on the
service instead of scanning. A scan newer than `SCAN_FRESH_SECONDS` (120 s) is reused. The
subtitle shows the scan's age, `scanning…` or `scan failed`, and `presence unknown` when it could
not tell.

### Model jobs

Harvest-and-archive and the roundup's enrich pool are app-owned daemon jobs
(`tui/background.start_daemon`), not writer work and not screen workers. A harvest hides its row as
`harvesting…`; an answer with nothing in it saves and archives nothing and toasts so. A session
already harvested skips the model and is only archived. Enrich results are applied as `ClassifyOp`
batches and merged only if the thread still exists, its `mtime` is unchanged and it still needs
classification. Model calls go through `models/proc.py`, which enforces the per-task timeout and
the process-group kill (see [models.md](models.md)). Without a model for the task, the action
toasts instead of starting.

### Screens and keys

App-wide (non-priority, so a focused `Input` still receives them as text):

| Key | Does |
|---|---|
| `1`–`7` | Home, Projects, Resume, Roundup, Prune, Harvests, Usage |
| `q` | leave (the barrier above) |
| `u` | undo the newest queued decision |
| `ctrl+r` | rescan (queued when one is running) |
| `ctrl+p` | command palette (off while a modal is open) |
| `escape` | back, on every screen but Home |

Lists move with `ctrl+j`/`ctrl+k` or the arrows, and bare `j`/`k` wherever no `Input` is on the
screen.

| Screen | Keys |
|---|---|
| Home | `enter` runs the highlighted step or resumes the highlighted in-flight session; `tab` moves between the two lists |
| Projects | `enter` opens a project's triage; `t` triage it with an agent |
| Triage | `a` archive · `h` harvest and archive · `g` keep going · `s` snooze · `K` keep long-term · `d` delete · `space` mark · `*`/`A` mark all · `enter` detail · `r` resume · `t` triage with an agent |
| Resume | the picker; `enter` picks, `ctrl+r` widens to this tree |
| Roundup | `enter` run (or, when finished, plan again) · `c` stop |
| Prune | `D` delete the listed candidates · `c` stop |
| Harvests | `enter` read · `p` publish the group with an agent · `P` publish all |
| Usage / palette output | `r` re-run · `c` cancel |

A decision key applies to the marked rows, or to the highlighted row when none is marked. Marks
clear only when a decision was submitted. Bulk archive and bulk delete confirm with the count;
bulk harvest is refused. A bulk decision skips and reports sessions their source cannot archive
or delete (OpenCode) and applies the rest. When a rescan removed the highlighted or a marked row,
the next decision key refuses once with "the list changed under you — press the key again".
`s` opens a snooze prompt that takes `3d`, `2w`, `tomorrow`, `next monday` (`fuzzytime.py`).

Home's next-step order is `state.next_steps`, a pure function: roundup when stale; snoozed
sessions that woke; undecided sessions active in the last 7 days; pending harvests; the oldest
project with a backlog not already covered; otherwise resume the most recent in-flight session.

### Roundup flow

The Roundup screen waits for a fresh scan, runs `roundup.plan_roundup` on a job, and shows the
plan. `enter` asks once, with the counts to delete, purge, archive and enrich. Above
`ENRICH_WARN_THRESHOLD` (25) pending sessions the prompt is a choice: enrich all, enrich the last
7 days, skip enrich, or cancel. Its title carries the same counts and Cancel is highlighted, so
`enter` alone runs nothing. The phases run as one `RoundupPhasesOp`, enrich as the app's job, then a
`StampRoundupOp`. The phases hold the roundup lock themselves; the driver releases it and re-acquires it
through `roundup.hold_lock` after phases, so another `rl roundup` can take it in that gap and
fail the run before enrich. `rl roundup` holds one lock across all of it. `home/roundup_run.py::RoundupDriver`
drives it; which step follows phases or enrich comes from `roundup.next_step`, shared with
`run_roundup`. `c` stops at the next item
boundary; a cancelled run is not stamped. Prune is a separate screen over the same candidate set
and runs as one `PruneOp` in batches.

### Handoffs and `run_intent`

Resume, "triage with an agent" and "publish with an agent" end in an `ExitIntent`
(`ResumeIntent` or `AgentHandoffIntent`), returned from `App.run()`. Only after the app has
exited does `launch.run_intent` act: a resume goes through `cmd_resume` with the chosen `Choice`,
exactly as `rl resume` would; a handoff checks the agent is installed and the directory exists,
then `exec`s the agent's `launch_argv(prompt)` there. Publish-all runs in the common ancestor of
the harvests' folders and asks first when that is `$HOME` or above.

### Palette

`ctrl+p` lists navigation, the current next steps and `PALETTE_VERBS`: `usage`, `usage
--breakdown`, `usage windows`, `harvests`, `overview --sessions --no-scan`, `triage
--unclassified --no-scan`, `doctor`, `config show`. Each runs as `python -P -m ringleader <argv>` in
its own process group (`-P`, so a `ringleader/` directory in the cwd cannot shadow the installed
package) and its captured output shows in `OutputScreen` (600 s limit, 2 M characters,
cancelled on quit). `PALETTE_EXCLUDED` names every other top-level verb with a reason, and a test
requires each to be in exactly one of the two.

### Read-only mode

`rl inbox --read-only` scans without persisting, refuses every write, starts no model call, and
turns exit intents into a dry run: `rl resume --dry-run` for a resume, the directory and argv for
a handoff. `scripts/tui_shot.py --registry PATH` always runs read-only on a copy.

### Print capture

The app calls `begin_capture_print(stdout=False, stderr=True)`. Lines printed to stderr from any
thread, such as the scanner's `skipped <source> …`, become warning toasts, coalesced to one per
second by `home/prints.py::PrintCoalescer`.

## Key entry points

- `commands/inbox.py` — `cmd_inbox`, the launcher and exit handling.
- `home/app.py` — `InboxApp`, `decide`, `harvest`, `go`, `_leave`.
- `home/roundup_run.py` — `RoundupDriver`, behind `InboxApp.start_roundup`/`cancel_roundup`.
- `home/jobs.py` — `JobBook`, behind `InboxApp.harvest` and `run_job`.
- `home/state.py` — `next_steps`, `project_queue`, `scope_rows`.
- `home/writer.py`, `home/overlay.py` — the mutation queue and the view.
- `cli.py` — `_opens_inbox`, the bare-`rl` rule.

## Gotchas

- **Background work is a daemon `threading.Thread` that calls `App.post_message`.** Never
  `@work(thread=True)` or `concurrent.futures`: their threads are joined at interpreter exit and
  hang the process after `q`.
- **Jobs belong to the app, not a screen.** Textual cancels a screen's workers when it is popped,
  which would orphan a write or a harvest.
- **`OptionList` loses its highlight on `set_options`.** `tui/rows.py::set_rows` restores it by
  key; removal posts no `OptionHighlighted`. Use `OptionList`, not `ListView`, for long lists.
- **Modals own their keys.** App bindings do not fire under a `ModalScreen`, so each modal binds
  its own `y`/`n`/`escape`/digits, and the palette is disabled over one.
- **`ctrl+r` on the Resume screen means "this tree"**, the picker's meaning, not rescan.
- **Navigate with `pop_screen()` in a loop.** `pop_until_active()` defers its pops and would pop
  the screen just pushed.
- **`app.query_one` searches the default screen**; query `app.screen` in tests and later code.
- **Time comes from the injected `Clock`.** Every rendered age uses it, which is what keeps
  goldens stable.
- **A picker `settle` never returns while the agent modal is open**; `tui_shot` bounds it at 5 s.
- **Textual owns the terminal**, so stderr is invisible while the app runs except through the
  print capture above.

## Why

One writer and an overlay make ordering, undo and "a decided row stays gone" properties of one
queue instead of coordination between screens. Daemon threads with `post_message` keep a
2,000-session registry responsive: first paint and key latency are measured by
[the inbox benchmark](../tests/inbox/README.md). The grace-and-undo delete trades a per-row
prompt for a 10 s window; the decision and its alternatives are in
[ADR 0014](../decisions/0014-rl-inbox-as-the-daily-home.md).

## Related

- [features/views-and-triage.md](../features/views-and-triage.md)
- [tui-testing.md](tui-testing.md) — goldens, `tui_shot`, pty runs
- [resume.md](resume.md) — the hosted picker and `exec_agent`
- [scanning-and-refresh.md](scanning-and-refresh.md) — `auto_refresh` and the roundup API
- [registry.md](registry.md)
