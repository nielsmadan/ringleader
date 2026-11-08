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
| `home/intents.py`, `home/launch.py` | exit intents and the pure handoff builders, and running one after the app has exited (the launch checks are shared with `rl handoff triage` in `ringleader/handoff.py`) |
| `home/keymap.py` | the `vim` and `circus` key presets (`inbox.keys`), the triage hint built from them, and `with_bindings` |
| `home/palette.py` | the command palette provider and its verb allowlist |
| `home/screens/` | `home`, `triage`, `projects`, `roundup`, `prune`, `harvests`, `detail`, `output`, `usage` |
| `tui/` | shared: `background` (daemon jobs), `modals`, `rows`, `keys`, `runner`, `snapshot` |

`commands/inbox.py::cmd_inbox` builds the app and runs it. After `run()` returns it aborts
background work, joins the writer, kills live model children, returns a crash's non-zero code, and
only then runs the exit intent through `launch.run_intent`.

### Layering

`home/` depends on `decisions`, `roundup`, `enrichment` and `revalidate`. Shared
archive, restore and sweep eligibility operations live in `lifecycle.py`; harvest prompt,
rendering and persistence live in `harvesting.py`. These modules do not import commands.
Lifecycle callers supply timestamps and receive structured archive warnings; CLI commands
print those warnings, while decisions and roundup include them in their result objects.
The Prune screen uses `lifecycle.due` for sweep eligibility.

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

### Rendering

Every change to `AppState` bumps its `version`. Writer events, scans, harvest and enrich
messages, and roundup state call `_rerender`, which runs at most once per refresh: it calls the
active screen's `sync` only when the version changed since the app last synced that screen.
Otherwise a screen with `render_status` (Home's status line, Roundup's run) redraws just that,
so roundup, enrich and prune progress never rebuild a list. Prune progress goes straight to the
Prune screen's bar through `op_progress`.

The view is memoized: `InboxApp.view()` and `view_threads()` are rebuilt only when the adopted
snapshot object or the set of hidden identities changes, and each rebuild gets a new serial.
`InboxApp.render_basis()` issues the one `RenderBasis` every screen compares against: that view
serial, the clock truncated to the minute, and the live ids, plus the exact time, which does not
take part in equality. `home_state()` is rebuilt when the basis's view or minute or the
pending-harvest count changes; the palette and the Home and Projects screens share that one
`HomeState`. Triage redraws its rows when the whole basis changed, Prune when its view or minute
did, Harvests when its groups or the minute did. `tui/rows.py::set_rows` then diffs against what
the `OptionList` shows: unchanged ids replace only the prompts that differ, up to
`MAX_PATCHED_REMOVALS` (64) in-order removals remove just those rows, and anything else rebuilds
the list. A removal combined with more than `MAX_PATCHED_CHANGE_SHARE_WITH_REMOVALS` (three
quarters) of the rows changed also rebuilds. Both limits come from timing patch against rebuild
on 2,000 rows with Textual 8.2.8: replacing the prompt of every row (about 78 ms) stays cheaper
than `set_options` (about 96 ms), so a minute tick that ages every row patches. A single removal
already costs about 76 ms, so with one the rebuild wins once about three quarters of the rows
changed. A Triage mark redraws only the rows it flipped.

### The writer and its ops

`Writer` is one daemon thread draining a queue in order. Consecutive ready ops of one kind
form a group of up to `GROUP_LIMIT` (`prune.DELETE_BATCH`, 25): marks (keep going, snooze,
keep), archives and deletes each run inside one registry transaction (`batch.run_batch`, see
[registry.md](registry.md)) that rechecks each session right before acting on it. Ops stay in
the queue while their group runs: the executor *claims* each one as its turn comes, which moves
it to running and delivers its `Started`. An op withdrawn before its claim — by `cancel(ticket)`,
the undo key or the discard confirm — is skipped and gets no `Finished`; only the claimed one is
`running`. Every claimed op gets its own `Finished` with its own outcome and generation; only
the last carries the reload snapshot. A group whose transaction fails before claiming anything
still finishes those ops with the failure. Harvest, prune, roundup, classify, stamp and reload
ops always run alone. The writer takes one `decisions.Executor` holding both `execute` and
`execute_group`; the app's default is `decisions.EXECUTOR`, a test can inject its own executor
object (which then groups too), and a plain callable is wrapped as `decisions.OneByOne`. Ops
are `ArchiveOp`, `DeleteOp`, `KeepGoingOp`, `SnoozeOp`, `KeepOp`, `HarvestSaveOp`, `PruneOp`,
`RoundupPhasesOp`, `ClassifyOp`, `StampRoundupOp` and `ReloadOp` (`decisions.py`). Screens call
`InboxApp` methods that build ops and `submit` them; they never write. `writer.waiting` counts
ops that have not started; `writer.unfinished` adds the ones claimed but not yet finished.

- Deletes carry a `not_before` of now plus `DELETE_GRACE_SECONDS` (10 s). Everything else runs
  at once.
- Undo (`u`, or `z` with the circus keys) calls `undo_last`: the newest queued batch that is not a reload, classify or stamp, going
  further back on repeated presses. An op that has started cannot be undone; an archive is
  reversed with `rl session unarchive`. When part of the batch already ran, the toast says so:
  `undone: 2 of delete 3 sessions (1 already applied)`.
- `cancel(ticket)` drops one op that has not started; `discard_pending` drops all of them.
  Cancelling the running op asks it to stop at its next item boundary; the flag resets when the
  next op is claimed, so it never leaks into the rest of a group.
- A delete still in its grace window does not hold up later ops: a ready mark or archive
  submitted after it runs first.
- In read-only mode every submit except a reload is refused with `read-only: <what> is disabled`.
- An unexpected exception in an op becomes an `internal error: <type>: <message>` outcome and the
  traceback is appended to `<state dir>/inbox-errors.log`, which rotates to
  `inbox-errors.log.1` once it reaches 1 MB. Inside a group, only the op that raised gets the
  internal error; when the whole group raises, every op in it gets the error and the traceback is
  logged once. `InboxApp(strict=True)` re-raises it instead.
- A registry write that fails after a file already moved reports it: "on disk, but the registry
  was not updated".

### Revalidation

Before every destructive op (archive, harvest-and-archive, delete, prune, sweep) the writer
reloads the thread under the lock the CLI command takes and refuses when the thread is gone, its
registry `mtime` differs from what the user saw, or the transcript's `(size, mtime_ns)` differs
from its `fileFingerprint`. A delete or prune also refuses a thread with no `fileFingerprint`
(`changed since you looked: no fingerprint, rescan`), and `scanner.filesystem_path` refuses to
delete a transcript, or a companion, that is not inside its source's session store. A sweep also refuses a transcript newer than `sweep.GRACE_SECONDS`.
Presence is probed inside the transaction, after the locks are taken, and again before an item
once the reading is over a second old: a running session is refused with a pointer to `ringleader-finish`, and a
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
service instead of scanning. In Resume, `ctrl+r`/`F5` requests a new service scan and the picker
waits for that attempt's outcome before replacing its rows. Requests survive leaving Resume;
the picker resolves the current generation and decision overlay on the UI thread. The inbox
owns scan notifications, including failures. A scan newer than `SCAN_FRESH_SECONDS` (120 s) is reused. The
subtitle shows the scan's age, `scanning…` or `scan failed`, and `presence unknown` when it could
not tell. Each scan passes the previous `HarvestSnapshot` to `harvests.snapshot_harvests`, which
reuses a harvest whose file still has the same `(mtime_ns, size)` and rereads the rest.

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

App-wide (non-priority except `ctrl+c`, so a focused `Input` still receives ordinary typing):

| Key | Does |
|---|---|
| `1`–`7` | Home, Projects, Resume, Roundup, Prune, Harvests, Usage |
| `q` | leave (the barrier above) |
| `u` (`z`) | undo the newest queued decision |
| `ctrl+r` | rescan (queued when one is running) |
| `ctrl+p` | command palette (off while a modal is open) |
| `escape` | back, on every screen but Home |
| `ctrl+c` | cancel the current dialog or go back like Escape; on Home, leave through the same barrier as `q` |

Lists move with `ctrl+j`/`ctrl+k` or the arrows, and bare `j`/`k` wherever no `Input` is on the
screen.

| Screen | Keys |
|---|---|
| Home | `enter` runs the highlighted step or resumes the highlighted in-flight session; `tab` moves between the two lists |
| Projects | `enter` opens a project's triage; `t` triage it with an agent |
| Triage | `a` archive · `h` harvest and archive · `g` keep going · `s` snooze · `K` keep long-term · `d` delete · `space` mark · `*`/`A` mark all · `enter` detail (also a click) · `r` resume · `t` triage with an agent; `enter` on the done state opens the next project |
| Resume | the picker; `enter` picks, `ctrl+t` widens to this tree, `ctrl+r`/`F5` refresh |
| Roundup | `enter` run (or, when finished, plan again) · `c` stop |
| Prune | `D` delete the listed candidates · `c` stop |
| Harvests | `enter` read · `p` publish the group with an agent · `P` publish all |
| Usage / palette output | `r` re-run · `c` cancel |

Those are the `vim` preset, the default. `config.inbox_keys` (`rl config set inbox.keys
vim|circus`) picks a preset from `home/keymap.py`: a `Keymap` holds the triage decision bindings
and the undo key, and `keymap.hint` builds the hint line from the hidden bindings, so the hint
cannot drift from the keys. `config.key_preset` is the one check of a stored value (`rl config
set` and `rl inbox` both use it). The `circus` preset matches the circus app's Inbox: `e` harvest,
`E` archive, `r` keep going, `h` snooze, `s` keep long-term, `#` delete, `x` mark, `*` mark all,
`enter`/`o` resume, `v` detail, `z` undo. `InboxApp` takes the keymap (`rl inbox` resolves it
from config, refusing an unknown preset with exit 2) and binds the undo key on itself; each
`TriageScreen` is constructed with it and merges the preset's keys into its own `_bindings`
(`keymap.with_bindings`, which keeps whole `Binding` objects), after the shared class bindings so
the footer order stays fixed. Textual has no public per-instance binding API, so both go through
the private `_bindings`. In both presets `enter` is a priority
binding, so the `OptionList`'s own select is reached only by a click, which opens the detail.

A decision key applies to the marked rows, or to the highlighted row when none is marked. Marks
clear only when a decision was submitted. Bulk archive and bulk delete confirm with the count;
bulk harvest is refused. A bulk decision skips and reports sessions their source cannot archive
or delete (OpenCode) and applies the rest. When a rescan removed the highlighted or a marked row,
the next decision key refuses once with "the list changed under you — press the key again".
The snooze key opens a prompt that takes `3d`, `2w`, `tomorrow`, `next monday` (`fuzzytime.py`).

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
cancelled on quit and whenever the screen is unmounted, so `1`–`7` away from Usage stop its
run). The output is written once per run, not on every state change. `PALETTE_EXCLUDED` names every other top-level verb with a reason, and a test
requires each to be in exactly one of the two.

### Read-only mode

`rl inbox --read-only` scans without persisting, refuses every write, starts no model call, and
turns exit intents into a dry run: `rl resume --dry-run` for a resume, the directory and argv for
a handoff. `scripts/tui_shot.py --registry PATH` always runs read-only on a copy.
`cmd_inbox` handles `--json` (and `--scan`) before the Textual check and rejects `--json` with
`--read-only`; that machine mode is specified in `json-contract.md`.

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
  key and posts `OptionHighlighted` only when the highlighted row changed; Textual's own removal
  posts none. Its visuals are built eagerly, about 20 µs a row, so never `set_options` a long
  list when `set_rows` or `replace_option_prompt_at_index` will do. Use `OptionList`, not
  `ListView`, for long lists.
- **Rendering a large `Static` blocks the UI thread.** The Detail conversation (up to 200 KB) is
  a read-only `TextArea` (`ConversationView`), which renders only the visible lines and
  re-wraps on resize; the arrows scroll it instead of moving a hidden cursor.
- **A screen's own `sync` is invisible to the app's version check**, so the first `_rerender`
  after a push or resume syncs once more. With the memoized view that second sync draws nothing.
- **Modals own their keys.** App bindings do not fire under a `ModalScreen`, so each modal binds
  its own `y`/`n`/`escape`/digits, and the palette is disabled over one.
- **Resume refreshes through the inbox scan service.** Its priority `ctrl+r`/`F5` bindings
  work from the filter input; `ctrl+t` widens to the directory tree.
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
