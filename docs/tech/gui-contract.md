# GUI contract (`rl gui`, version 1)

Modules: `ringleader/contract.py` (pure documents), `ringleader/contract_io.py` (list, scan, tail and
argument parsing for decide), `ringleader/guarded.py` (decide's work),
`scripts/gui_world.py` (a synthetic world for development and QA).

## Purpose

`rl gui` is the machine interface of the circus desktop app. Every subcommand prints exactly one
JSON document on stdout and nothing else. It is not for hand use and is versioned: every document
carries `"version": 1`. The group exists to keep the app's logic out of ringleader (the client
decides what to show) while keeping ringleader's rules (revalidation, presence, lifecycle,
locks) in one place.

## Commands

```text
rl gui list   [--registry=PATH]
rl gui scan   [--registry=PATH]
rl gui tail   ID [ID ...] [--max-chars=N] [--registry=PATH]
rl gui decide ACTION ID --mtime=MTIME [--fingerprint=FP] [--until=ISO] [--registry=PATH]
```

`--registry` defaults to the registry under `$XDG_DATA_HOME/ringleader`. IDs are qualified:
`<source>:<sessionId>` (for example `claude-code:3f2a…`), the same form as `SessionRow.id`.

- **list** reads the registry once and prints the inbox rows (see below). It never scans.
- **scan** runs `refresh.refresh_registry` (persisting) and prints how many threads were added
  and removed and which sources were unavailable.
- **tail** takes 1 to 16 distinct ids; more, or a repeated id, is an argparse error.
  `--max-chars` applies per item (default 200000, allowed 1000 to 2000000). Items come back in
  request order. One unreadable item never fails the batch: its `unavailable` field says why
  and its `text` is `""`. Each item also carries the registry thread's `mtime` and
  `file_fingerprint` at read time (both `null` when the id is not in the registry). A client
  caches a tail under the item's own `(id, mtime, file_fingerprint)`, so a tail read after a
  rescan changed the thread never poses as the old row's tail.
- **decide** applies one decision to one session; see Decide semantics.

## Output and exit codes

- stdout holds one document, written by one emitter: `sys.stdout.flush()`, then
  `sys.stdout.buffer.write(gui.encode_document(doc))`, then flush. `encode_document` is
  `(json.dumps(doc, ensure_ascii=False) + "\n").encode("utf-8", "replace")`, so a lone UTF-16
  surrogate from a transcript becomes `?` instead of crashing the process.
- All work runs inside `contextlib.redirect_stdout(sys.stderr)` (argument parsing too), so stray
  prints from library code go to stderr. The emitter runs after that block.
- Exit 0 when the requested document was printed, including a `decide` outcome whose status is
  `refused`, `empty` or `failed`.
- Exit 1 with `{"version": 1, "kind": "error", "message": …}` for an expected failure:
  `OSError`, `ValueError` (which covers `AmbiguousSession`), `KeyError`, `RuntimeError` (which
  covers `RegistryConflict`).
- Exit 2 for argparse usage errors: usage on stderr, no JSON. An `rl` without the `gui` group
  prints `rl: unknown command 'gui'` to stderr and exits 2; the client maps that to "rl too old".
- SIGTERM during `decide` exits 143 (see below).

## Startup cost

The app calls `rl gui list` often, so the read path stays cheap.

- `cli.main` dispatches `argv[0] == "gui"` to `contract_io.GUI_COMMANDS` before the command table is
  imported (`cli._table()` imports `ringleader.commands` on first use).
- `contract_io.py` and `guarded.py` live in the top-level package, not in `ringleader/commands/`:
  importing any module there runs `commands/__init__.py`, which imports every command.
- `decide` imports `guarded` (and with it `decisions`, `revalidate`, `agents`, `models`) on
  demand inside `cmd_gui_decide`. `list|scan|tail` never load them.
- `home/state.py` needs only `roundup_clock.is_stale`, so `is_stale` lives in the light module
  `ringleader/roundup_clock.py` (`roundup.py` re-exports it).
- Modules that must not be imported on the `list|scan|tail` path: `textual`,
  `ringleader.portability`, `ringleader.commands`, `ringleader.commands.conversion`,
  `ringleader.home.app`, `ringleader.roundup`, `ringleader.decisions`,
  `ringleader.enrichment`, `ringleader.models`. `tests/test_gui_perf.py` pins the list, the
  dispatch path and the time budget for `list`.
- `list` is bounded for 2 000 threads. Each call loads the registry exactly once (`scan` also
  reads it once before the refresh to record identities) and does constant work per row: there
  is no `find_session` or other scan of all threads inside a per-row loop. `tail` does one
  `find_session` per requested id (at most 16).

## Documents

### `list`

| Key | Type | Meaning |
|---|---|---|
| `version` | `1` | Contract version. |
| `kind` | `"list"` | Document kind. |
| `generated_at` | string | `now` as ISO 8601 UTC. |
| `registry` | string | `str(Path(--registry).resolve())`. |
| `recent_days` | number | `home.state.RECENT_DAYS` (7). |
| `presence` | `{state: string, problem: string or null}` | `state` is `"ok"` or `"unknown"` (hooklinesinker could not answer); `problem` is the reason or `null`. |
| `roundup` | `{last_at: string or null, age_human: string or null, stale: boolean, stale_hours: number}` | `last_at` is the registry's `lastRoundupAt` or `null`; `age_human` is `null` when never run (or unparsable); `stale` is `roundup_clock.is_stale`; `stale_hours` comes from config. |
| `sessions` | `SessionRow[]` | See below. |

Membership is `gui.inbox_threads`: `overview.triage_inbox` over `home.state.visible_threads`
(filtered sessions excluded, woke threads included), sorted by parsed `mtime` (a naive time is
treated as UTC) then by id, oldest first. Splitting into sections, ordering within a split and
project grouping belong to the client; the rule that decides which side a row falls on is
`in_recent` and `woke`.

`SessionRow` keys (alphabetical in `gui.SESSION_ROW_KEYS`):

| Key | Type | Meaning |
|---|---|---|
| `id` | string | `registry.qualified_id`, `<source>:<sessionId>`. |
| `source` | string | `resume.source_of`, e.g. `claude-code`, `codex`. |
| `agent` | string | `resume.agent_name(source)`, e.g. `claude`. |
| `session_id` | string | The agent's own session id. |
| `label` | string | `resume.label`. |
| `project` | string or null | The thread's cwd. |
| `project_short` | string | `home.state.short_path(project)`. |
| `project_key` | string | `overview.group_key`. |
| `project_label` | string | `home.state.project_label`. |
| `git_project_key` | string or null | Git project identity. |
| `git_project_label` | string or null | Git project display name. |
| `git_branch` | string or null | Branch at the last scan. |
| `last_prompt` | string or null | Clipped to 500 characters (see below). |
| `what_it_is` | string or null | Clipped to 500 characters. |
| `status` | string | `overview.effective_status`. |
| `mtime` | string | ISO 8601 with offset, exactly as in the registry. |
| `age_days` | number | `round(overview.age_days(mtime, now), 1)`. |
| `age_human` | string | `overview.age_human`. |
| `size_bytes` | number or null | Transcript size. |
| `size_human` | string | `overview.human_size`. |
| `message_count` | number | `messageCount`, or 0. |
| `file` | string or null | Transcript path. |
| `decision` | string or null | The recorded decision (`keep`, `harvested`, …). |
| `reviewed_at_mtime` | string or null | `mtime` the user last marked "keep going" at. |
| `snoozed_at` | string or null | When the snooze was set. |
| `snoozed_until` | string or null | When it ends. |
| `kept_at` | string or null | When it was kept long-term. |
| `finish_on_exit` | boolean | Marked by the ringleader-finish skill. |
| `file_fingerprint` | string or null | `fileFingerprint`; the stamp `decide` revalidates against. |
| `running` | boolean or null | `null` when presence is unknown. |
| `in_recent` | boolean | The unrounded age is at most `recent_days`. `age_days` is rounded to one decimal, so do not recompute this from `age_days`. |
| `woke` | boolean | `home.state.is_woke`: a snooze that has expired. |
| `lifecycle_block` | string or null | Why archive, and harvest (whose save step archives), cannot touch the transcript (the message of the `RuntimeError`, `ValueError` or `KeyError` raised by `scanner.filesystem_path(thread, "archive")`; for a `KeyError` that is the quoted key, not a sentence), else `null`. It is not a delete pre-check: `decide delete` can still refuse a row whose block is `null`, for example when the transcript or a companion lies outside its source's session store. |
| `badges` | string[] | `resume.badges`: any of `filtered`, `snoozed`, `kept`, `finishing`, `harvested`, `abandoned`, `drifted`. |

The 500-character clip (`gui.clip`, `LIST_TEXT_MAX_CHARS`) keeps the list small: text longer
than the limit is cut to the first 499 characters plus `…`; `None` stays `None`. The full text
lives in the tail.

### `scan`

`{version: 1, kind: "scan", scanned_at: string, added: number, removed: number,
unavailable_sources: string[]}`: `scanned_at` is ISO 8601 UTC; `added` and `removed` are counts of
set differences of thread identities before and after the refresh; `unavailable_sources` is sorted.

### `tails`

`{version, kind: "tails", items: TailItem[]}`. `TailItem`:

| Key | Type | Meaning |
|---|---|---|
| `id` | string | The requested id. |
| `text` | string | `export.render_tail(thread, max_chars)`; `""` when unavailable. |
| `max_chars` | number | The limit applied. |
| `truncated` | boolean | `len(text) >= max_chars`. |
| `unavailable` | string or null | `"no longer in the registry"`, `"ambiguous session id"`, `"transcript not found"` or `"could not read the transcript (<ExceptionType>)"`. |
| `mtime` | string or null | The thread's `mtime` when the tail was read. |
| `file_fingerprint` | string or null | The thread's `fileFingerprint` when the tail was read. |

### `outcome` (decide)

| Key | Type | Meaning |
|---|---|---|
| `version`, `kind` | `1`, `"outcome"` | |
| `action` | string | The requested action. |
| `id` | string | The requested id. |
| `status` | string | `ok`, `refused`, `empty` or `failed`. |
| `reason` | string or null | For `refused`: the first refusal reason. Otherwise `null`. |
| `message` | string | Human-readable. |
| `wrote` | boolean | Something changed on disk or in the registry (true even for a partial failure). |
| `path` | string or null | Archive destination or saved harvest path. |
| `warnings` | string[] | Non-fatal problems (for example companion files that could not be removed). |

`error` documents are `{version: 1, kind: "error", message: string}`.

## Decide semantics

`ACTION` is one of `harvest`, `archive`, `delete`, `snooze`, `keep`, `keep_going`.
`--mtime` is required and is the `mtime` the user saw. `--until` is required for `snooze` and
rejected by argparse for every other action. An ID without a `:` is an argparse error.

Status mapping (`gui.from_outcome`): `decisions.Outcome.ok` gives `ok`; not ok with a non-empty
`refused` gives `refused` with the first reason; any other not-ok gives `failed`. `empty` is
produced only by harvest.

| Action | Operation | Revalidated | Presence checked |
|---|---|---|---|
| `archive` | `ArchiveOp` | yes, stamp required | yes |
| `delete` | `DeleteOp` | yes, stamp required | yes |
| `harvest` | `HarvestSaveOp` (after the model call) | yes, stamp required | yes |
| `snooze` | `SnoozeOp` | no | no |
| `keep` | `KeepOp` | no | no |
| `keep_going` | `KeepGoingOp` (`mark_reviewed` at `--mtime`) | no | no |

- **Revalidation** (`revalidate.changed_reason`) compares `--mtime` with the registry thread's
  `mtime` and, when `--fingerprint` carries a content stamp, the transcript's size and mtime
  in nanoseconds on disk. A mismatch is a refusal with reason `changed since you looked`; a
  session that left the registry is refused with `no longer in the registry`. When a stamp is
  given, the disk check can also refuse with `transcript is missing`, the `RuntimeError` text of
  `scanner.filesystem_path`, or an `OSError` message, so clients should treat the reason as
  free text.
- **Fingerprint requirement.** `archive`, `delete` and `harvest` are refused with
  `revalidate.NO_STAMP` (`changed since you looked: no fingerprint, rescan`) when
  `--fingerprint` is missing or is not a five-field numeric fingerprint, before the registry
  is read.
- **Presence.** The probe samples `hooklinesinker` (`presence.PresenceClient`) *after* the lock
  is taken, so the sample is taken right before the move or unlink, not before a possibly long
  wait for the lock. Agent sessions take no ringleader lock when they start, so a session that
  starts between the sample and the move is not caught. Unknown presence is treated as running
  (`unknown="refuse"`): the refusal reads `can't tell whether … is running`. A running session
  is refused with `… is running — finish it from inside with the ringleader-finish skill`.
- **Lifecycle.** `archive`, `delete` and `harvest` on a transcript that is not a plain file
  (for example an OpenCode database row), or on a thread whose recorded native roots are
  corrupt, are refused before any work with the `decisions.lifecycle_reason` text (`cannot
  harvest …` for harvest). `delete` is also refused when the transcript or a companion lies
  outside its source's session store.
- **Locks.** `archive` and `harvest` run under `stateio.publish_lock()`; `delete` runs inside
  `registry.mutate_registry`; `snooze`, `keep` and `keep_going` use `mutate_registry` only.
  Harvest's save step reuses the publish lock through `save_harvest_unlocked`. There is no outer
  lock around any decide operation: `stateio.publish_lock` opens a new file descriptor and takes
  `LOCK_EX` each time, so it is not reentrant, and an outer `publish_lock` around an op that
  takes it itself (`archive`, the harvest save) deadlocks in the same process. For harvest the
  model call can also take minutes and must not block other writers, so the harvest is gated,
  then the model runs unlocked, then the save step takes the lock and revalidates again.
- **Harvest flow.** Gate (lifecycle, revalidation, presence, in that order, each a refusal); if
  the thread's `decision` is already `harvested` the model is skipped and the save step only
  archives; otherwise the configured harvest model is required (`no harvest model configured —
  run rl config` is a refusal), a model failure is `failed`, and a result with no summary and
  no list items is `empty` (nothing written). Otherwise the markdown is saved, the thread is
  marked harvested and archived: `ok` with `path` set to the saved harvest.
- **SIGTERM.** `cmd_gui_decide` installs a handler that exits 143, and its `finally` kills any
  model child processes (`models.proc.kill_all_children`) and restores the previous handler, so
  cancelling a harvest from the app does not leave a model running. SIGTERM is blocked
  (`stateio.sigterm_deferred`) for the whole archive transaction in `decisions.execute_group`,
  and from the harvest save through the commit in harvest, so a signal there exits only after the
  archive is recorded, with no outcome printed.
- **Snooze.** `--until` must be ISO 8601 with a timezone offset and later than now; it is
  stored normalized to UTC (`gui.normalize_until`, `astimezone(UTC).isoformat()`) because `overview.snooze_active` compares ISO strings (`now_iso < until`), which orders correctly only when both sides are in the same zone; do not keep the user's offset. Failures are refusals: unparsable input gives
  `--until is not an ISO 8601 time: …`, a naive time `--until needs a timezone offset`, a past
  time `snooze time must be in the future`.
- **Missing session on a non-revalidated op.** `keep`, `keep_going` and `snooze` do not
  revalidate; if the session has left the registry, `decisions.execute` turns the `KeyError` into
  a not-ok outcome with no refusal, which is reported as `failed` (message `… no longer in the
  registry`). It is not special-cased.
- A decision is never retried automatically by `rl`.

## What the commands never do

None of them runs a scan except `scan`. None of them sweeps, prunes, enriches or runs roundup.
`list` and `tail` never write.

## Design decisions

- **One process per call, no daemon.** The client spawns `rl gui …` each time; startup cost is
  kept down by lazy imports (see Startup cost). A persistent `rl gui serve` speaking the same
  documents over stdin/stdout is the next lever if cold spawns still feel slow; it is not built.
- **Same ops as the TUI.** `decide` runs the `decisions.py` ops, so revalidation, presence and
  locks are not reimplemented. The only stricter rule is the required fingerprint for
  `archive`, `delete` and `harvest`.
- **No delete grace here.** `decide delete` acts immediately; the client owns the undo window
  (10 s, the TUI's `DELETE_GRACE_SECONDS`).
- **The client keeps at most one decision per session in flight** and never retries; `rl` has no
  outer lock to serialize them (see Locks).
- **`list` is all or nothing.** A registry row without a string `mtime` makes `list` exit 1 with
  an `error` document, as the TUI's state loading fails on the same row; ordering is defined by
  `mtime`, so there is no per-row skip.
- **`lifecycle_block` is the archive/harvest lifecycle only.** Changing its shape, or adding a
  delete pre-check field, is a contract change (version bump on both sides plus goldens).
  `decide delete` stays the guard for delete refusals.

## Versioning

`version` is `1`. Adding a key to a document, or a new `unavailable`/`reason` string, is a
compatible change only if the client already ignores unknown keys; renaming, removing or
changing the type of a key, or changing the meaning of a status, requires `version: 2` and a
client that checks it. Key sets are pinned by `gui.SESSION_ROW_KEYS` and the golden documents. Change the contract and the circus client (types, Rust envelope, goldens) together.

## Golden documents

`tests/golden/gui-list.json`, `gui-tails.json`, `gui-outcome-ok.json` and
`gui-outcome-refused.json` are produced in-process by `tests/test_gui_golden.py` from a
deterministic fixture (`threads=20`, `transcripts=False`) with the temporary root replaced by
`/fixture` (also inside the Claude project-directory slug). In that fixture the transcripts do
not exist, so found tail items are `unavailable: "transcript not found"`. Regenerate with:

```sh
RINGLEADER_UPDATE_GOLDEN=1 uv run python -m pytest tests/test_gui_golden.py
```

The circus repository keeps byte-identical copies (`just sync-golden` copies, `just
golden-check` compares), so change a golden only together with the contract and the client.

## Synthetic world

`scripts/gui_world.py --root DIR [--threads N] [--exports]` builds a disposable world for
development and QA of the app: a `tui_fixture` registry with real Claude transcripts, a
`claude-settings.json`, and `bin/` stubs (`claude` exits 97, `fake-harvest` answers harvests,
empty for any project directory ending in `/proj-00`), plus a fake `hooklinesinker` that reports
one running session. Its config points `ai_command_prefix` at `fake-harvest`. It refuses to build
inside `~/.local/share`, `~/.claude`, `~/.codex`, `~/.factory` or `~/.pi`, and refuses to replace
any existing directory without a `.gui-world` marker, and any path that is not a directory. `--exports` prints `export KEY=value` lines
for the six data variables, `RINGLEADER_CLAUDE_SETTINGS`, and a `PATH` that puts `bin/` first.
Source them (and `unset OPENCODE_DB`) before running `rl`; `--registry` alone is not isolation.

## Testing

`tests/test_gui.py` (documents), `tests/test_rl_gui.py` (commands end to end),
`tests/test_gui_perf.py` (imports and timing), `tests/test_gui_golden.py` (goldens) and
`tests/test_gui_world.py` (the world script).
