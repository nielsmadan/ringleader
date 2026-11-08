# 0014: Make `rl inbox` the daily home, built on one writer

## Context

The first `rl inbox` was a single `ListView` of undecided sessions. On a live registry of about
1,900 threads it froze: the roundup ran synchronously on the event loop, enrich made about 1,839
sequential model calls, and one `ListItem` per session gave an 18 s first render and about 2 s
per key. Its deletes were staged until exit, which hid a long-lived queue of irreversible work.

The daily loop was also spread across verbs and skills: `rl overview` to look, `rl triage` or the
triage skill to decide, `rl roundup` to refresh, `rl resume` to re-enter, `rl harvests` and the
publish skill to integrate. Nothing said which to run next.

## Decision

**`rl inbox` is rewritten as the full-screen daily home**, and bare `rl` opens it. Home shows the
counts, one recommended next step plus the ordered list (roundup when stale, expired snoozes,
recent undecided sessions, pending harvests, the next project, else resume), the sessions in
flight, and what wakes soon. Screens: Home, Projects, Resume (the picker hosted as a screen),
Roundup, Prune, Harvests, Usage, and a triage screen per project. Fast, mechanical decisions are
native; judgment work is handed to an agent.

**One writer.** `home/writer.py::Writer` owns every registry mutation and filesystem move: one
daemon thread draining a queue of ops, one at a time. Screens never write. Writer, scans and
model jobs belong to the app, not to a screen, because Textual cancels a screen's workers when it
is popped.

**Generations and an overlay.** Every registry snapshot carries the writer's count of applied
writes; a snapshot is adopted only when its generation is not older than the current one. The
view is the adopted snapshot minus an overlay of identities with a decision queued, running, or
applied after that snapshot. A decided row cannot come back from a scan or another decision's
reload.

**Revalidation and presence.** Right before every destructive op the writer reloads the thread
under the lock the CLI takes and refuses when it is gone, its registry `mtime` or the
transcript's `fileFingerprint` changed since the user saw it, or a sweep target is not quiet.
`rl prune --delete` gets the same disk check. Presence is read fresh before each destructive op;
a session that is running is refused, and "cannot tell" is refused too, never read as idle.
`rl roundup` keeps treating "cannot tell" as allowed.

**Leave barrier.** `q`, a resume and every agent handoff go through one path. If the writer is
busy or a harvest is in flight, the delete grace is flushed, a banner shows, and the app exits
when the writer drains. A second `q` offers to discard what has not started; the change being
written still finishes. Nothing is exec'd while the writer is busy.

**Deletes and bulk actions.** There is no prompt per delete: `d` queues it with a 10 s grace and
`u` undoes it, going further back on repeated presses. Space, `*` and `A` mark rows; a decision
key applies to the marks. Bulk archive and bulk delete confirm with the count, and bulk harvest
is refused. The settings are in `home/policy.py`; per-delete confirmation is
`CONFIRM_EACH_DELETE = True` with `DELETE_GRACE_SECONDS = 0.0`.

**Keep going marks the session reviewed.** It uses `registry.mark_reviewed` at the `mtime` the
user saw, so later activity resurfaces the session. Nothing is selected for the day.

**Handoffs exit, then exec.** Triaging a project with an agent and publishing harvests quit the
app and run the chosen agent CLI in the right directory with a prompt naming the skill. Publish-all
runs in the common ancestor of the harvests' folders and asks first when that is `$HOME` or above.

*Amended 2026-11-08:* the keys above are the `vim` preset, the default. `rl config set inbox.keys
circus` switches the triage keys to the circus app's Inbox (`#` delete, `z` undo, `x` mark, `*`
mark all, `enter` resumes and `v` opens the detail), and `rl handoff triage` makes the triage
handoff available outside the TUI, with the same launch checks (`ringleader/handoff.py`).

**Model calls are bounded.** Enrich and harvest run as app-owned daemon jobs through
`models/proc.py`: own process group, per-task timeout, cancellable, reaped on exit. Enrich has its
own model setting (`rl config set enrich.model`). Its default (`claude` → `haiku`) is provisional
until the benchmark's Claude legs run; see [the enrich record](../tests/enrich/README.md).

## Consequences

- Bare `rl` changes behaviour only when stdin and stdout are TTYs, `TERM` is not `dumb`, and no
  agent session is detected. Everywhere else it prints usage as before.
- The skills are unchanged and still the route for naming, harvesting into docs and publishing.
- The TUI never names a session without a model; naming by hand stays with the skills.
- A destructive action needs presence. Without `rl data presence install`, archive, delete and
  prune are refused in the TUI; `rl roundup` still works.
- The command palette (`ctrl+p`) is an allowlist of flat, non-interactive, non-destructive verbs;
  a test requires every top-level verb to be either allowed or excluded with a reason.
- `rl roundup` keeps no per-delete confirmation. Its plan is the confirmation, and each item is
  rechecked at execution. The TUI shows the same plan and asks once.
- Roundup holds the roundup lock through enrich, so a second roundup, CLI or TUI, is refused for
  that long.
- The old inbox's macOS "roundup complete" notification is gone, so a long roundup in the TUI
  ends without a signal outside the terminal.
- `rl inbox --read-only` browses without writing and prints exits as a dry run.
- Responsiveness is measured by [a benchmark](../tests/inbox/README.md): first paint about 0.1 s
  and key medians under 35 ms at 2,000 threads. Cold start was measured at about 1.2 s and is
  recorded, not budgeted.
- The `selectedForDay` registry field is no longer read; new sessions still get it as `null`.

*Amended 2026-10-17:* the writer no longer runs every op alone. Consecutive ready ops of one kind
run as a group of up to 25 under one locked registry transaction, and each item is still
rechecked individually. Presence is read fresh under the lock before a group's destructive ops,
not once per op. See [inbox.md](../tech/inbox.md#the-writer-and-its-ops).

## Alternatives considered

**Fix the old inbox in place.** The freeze came from the structure, not one call: synchronous work
on the loop and a widget per session. An `OptionList`, background workers and a single writer
touch every line, so it was rewritten.

**Prompt before every delete.** The safer default, and one setting away. Rejected as the default
because triage is mostly deleting, and a prompt per row costs more attention than a 10 s window
that `u` can reopen.

**Let screens write directly.** Simpler per screen, but popping a screen cancels its workers and
two screens can race the registry. One writer makes ordering and undo a property of one queue.
