# JSON contract (version 1)

Modules: `ringleader/contract.py` (pure documents), `ringleader/contract_io.py` (list, scan, tail,
the JSON emitter and the guarded-path runner), `ringleader/guarded.py` (the guarded decisions),
`ringleader/guard_args.py` (the shared `--json` / `--if-*` flags), `ringleader/handoff.py` (the
agent handoff plan), `ringleader/devtools/gui_world.py` (a synthetic world for development and
QA, shipped in the wheel).

## Purpose

Some `rl` commands have a machine mode for the circus desktop app. In machine mode a command
prints exactly one JSON document on stdout and nothing else. The mode is for programs, not hands,
and is versioned: every document carries `"version": 1`. It exists to keep the app's logic out of
ringleader (the client decides what to show) while keeping ringleader's rules (revalidation,
presence, lifecycle, locks) in one place.

## Commands

```text
rl --version
rl inbox --json [--scan] [--registry=PATH]
rl data export ID [ID ...] --json [--max-chars=N] [--registry=PATH]
rl handoff triage --project=KEY --agent=AGENT --dry-run --json [--registry=PATH]

rl session archive       ID GUARD
rl session delete        ID GUARD
rl session harvest       ID --archive GUARD
rl session snooze        ID --until=ISO GUARD
rl session keep          ID GUARD
rl session mark reviewed ID GUARD

GUARD := [--json] --if-mtime=MTIME [--if-fingerprint=FP] [--registry=PATH]
```

`--registry` defaults to the registry under `$XDG_DATA_HOME/ringleader`. IDs are qualified:
`<source>:<sessionId>` (for example `claude-code:3f2a…`), the same form as `SessionRow.id`.

- **`rl --version`** prints `rl X.Y.Z` (the installed package version, from `pyproject.toml`)
  and exits 0. It is plain text, not a document. An `rl` older than 0.2.0 exits 2 with `unknown
  command '--version'`, which a client can read as "needs an update".
- **`rl inbox --json`** reads the registry once and prints the inbox rows (see Documents). With
  `--scan` it first runs `refresh.refresh_registry` (persisting) and the list it prints carries a
  `scan` summary. Without `--scan` it never scans.
- **`rl data export --json`** takes 1 to 16 distinct IDs; more, or a repeated ID, is a usage
  error. `--max-chars` applies per item (default 200000, allowed 1000 to 2000000). Items come back
  in request order. One unreadable item never fails the batch: its `unavailable` field says why
  and its `text` is `""`. Each item also carries the registry thread's `mtime` and
  `file_fingerprint` at read time (both `null` when the ID is not in the registry). A client caches
  a tail under the item's own `(id, mtime, file_fingerprint)`, so a tail read after a rescan
  changed the thread never poses as the old row's tail.
- **`rl handoff triage --dry-run --json`** plans the TUI's "triage with an agent" (`t`) for one
  project and prints the directory and argv without launching anything (see `handoff` below). The
  client launches the same command without `--dry-run --json` in a terminal: `rl` then `chdir`s
  to that directory and `execvpe`s the agent, as `rl resume` does.
- **The six session verbs** apply one decision to one session when called with the guard; see
  Guarded decisions. Without `--json` and without any `--if-*` flag they are the plain verbs and
  behave as they always did.

## Argument rules

Each violation is a usage error: exit 2, usage on stderr, nothing on stdout.

- `inbox`: `--scan` needs `--json`; `--json` cannot be combined with `--read-only`.
- `handoff triage`: `--project` and `--agent` are required; `--project` is a `project_key`, or a
  project label that exactly one waiting project has; `--agent` is one of the launchable agents,
  by source name or display name (`claude-code` or `claude`); `--json` needs `--dry-run`.
- `data export`: the positional is `ID [ID ...]`. Two or more IDs without `--json` is
  `more than one ID needs --json`, and `--max-chars` without `--json` is `--max-chars needs
  --json`; a single ID without `--json` is the plain export, unchanged. `--out` cannot be
  combined with `--json`. With `--json`: 1 to 16 distinct IDs and `--max-chars` within range.
- Guarded path (selected by `--json`, `--if-mtime` or `--if-fingerprint` on a session verb):
  `--if-mtime` is required (`--json` or `--if-fingerprint` alone is a usage error; an empty
  `--if-mtime=` counts as given). The ID is required and must contain `:`.
- Rejected on the guarded path: `archive --archive-root` and `--summary-file`; `harvest
  --current`, `--from-file` and `--dry-run`; `snooze --for`. The error names the flag.
- `harvest` needs `--archive` on the guarded path (the guarded harvest saves and archives), and
  `--archive` without `--if-mtime` is a usage error. `snooze` needs `--until`.
- `keep --yes` is accepted and ignored on the guarded path. `snooze`, `keep` and `mark reviewed`
  accept `--if-fingerprint` and do not use it.
- `--until` that is unparsable, naive or in the past is a `refused` outcome, not a usage error.

## Output and exit codes

- stdout holds one document, written by one emitter (`contract_io.emit`): `sys.stdout.flush()`,
  then `sys.stdout.buffer.write(contract.encode_document(doc))`, then flush. `encode_document` is
  `(json.dumps(doc, ensure_ascii=False) + "\n").encode("utf-8", "replace")`, so a lone UTF-16
  surrogate from a transcript becomes `?` instead of crashing the process.
- All work runs inside `contextlib.redirect_stdout(sys.stderr)` (`contract_io.run_json`), so stray
  prints from library code go to stderr. The emitter runs after that block.
- Exit 0 when the requested document was printed, including a guarded outcome whose status is
  `refused`, `empty` or `failed`.
- Exit 1 with `{"version": 1, "kind": "error", "message": …}` for an expected failure:
  `OSError`, `ValueError` (which covers `AmbiguousSession`), `KeyError`, `RuntimeError` (which
  covers `RegistryConflict`). Registry loads, presence probes and refreshes all run inside
  `run_json`, so the router's own `rl: …` handler never sees them.
- Exit 2 for usage errors: usage on stderr, no JSON. An `rl` that predates the contract exits 2
  with `unrecognized arguments: --json` or `unknown command` for `rl inbox --json`.
- SIGTERM during a guarded decision exits 143 (see Guarded decisions).

Without `--json` a guarded verb prints for humans:

- `ok` and `empty`: the message on stdout, then `path` on stdout when set and the message does not
  already contain it (harvest's does). Exit 0.
- `refused` and `failed`: `rl-<verb>: <message>` on stderr, then `path` on stderr when set. Exit 1.
- Warnings: `rl-<verb>: <warning>` on stderr.
- The expected exceptions above: `rl-<verb>: <error>` on stderr, exit 1.

`<verb>` is `archive`, `delete`, `harvest`, `snooze`, `keep` or `mark-reviewed`.

## Startup cost

The app calls `rl inbox --json` often, so the read path stays cheap.

- The command table is imported on first use (`cli._table()` imports `ringleader.commands`); its
  `__init__` is light, and each command module is loaded lazily when it is dispatched.
- `contract_io.py`, `guarded.py` and `guard_args.py` live in the top-level package, not in
  `ringleader/commands/`: importing any module there runs `commands/__init__.py`.
- `inbox`, `export` and the six session verbs import `contract_io` only after argument parsing
  selected a machine mode, so plain invocations never load it. `contract_io.run_guarded` imports
  `guarded` (and with it `decisions` and `models`) on demand; `inbox --json` and `export --json`
  never load them.
- The list counts harvests with `harvests.pending_count`, which lists the harvest directory and
  reads no file.
- `home/state.py` needs only `roundup_clock.is_stale`, so `is_stale` lives in the light module
  `ringleader/roundup_clock.py` (`roundup.py` re-exports it).
- Modules that must not be imported on the `inbox --json`, `inbox --json --scan` and `export
  --json` paths: `textual`, `ringleader.portability`, `ringleader.commands.conversion`,
  `ringleader.home.app`, `ringleader.roundup`, `ringleader.decisions`, `ringleader.enrichment`,
  `ringleader.models`. `rl inbox --json` returns before the Textual import check.
  `tests/test_contract_perf.py` pins the list, the dispatch paths, the plain-verb negative and the
  time budget for the list.
- The list is bounded for 2 000 threads. Each call loads the registry exactly once (`--scan` also
  reads it once before the refresh to record identities) and does constant work per row: there
  is no `find_session` or other scan of all threads inside a per-row loop. Export does one
  `find_session` per requested ID (at most 16).

## Documents

### `list`

| Key | Type | Meaning |
|---|---|---|
| `version` | `1` | Contract version. |
| `kind` | `"list"` | Document kind. |
| `generated_at` | string | `now` as ISO 8601 UTC. |
| `registry` | string | `str(Path(--registry).resolve())`. |
| `recent_days` | number | `home.state.RECENT_DAYS` (7). |
| `presence` | `{state: string, problem: string or null, code: string or null}` | `state` is `"ok"` or `"unknown"` (hooklinesinker could not answer); `problem` is the reason or `null`; `code` is `null` when `ok`, else `not_installed` (no hooklinesinker binary at its active path: run `rl data presence install`) or `failed` (installed, but it gave no answer). The set is closed (`contract.PRESENCE_CODES`). |
| `roundup` | `{last_at: string or null, age_human: string or null, stale: boolean, stale_hours: number}` | `last_at` is the registry's `lastRoundupAt` or `null`; `age_human` is `null` when never run (or unparsable); `stale` is `roundup_clock.is_stale`; `stale_hours` comes from config. |
| `scan` | `ScanSummary` or null | `null` unless the list was produced by `--scan`; see `scan` below. |
| `agents` | `Agent[]` | The agents `rl` can launch, in `scanner.SOURCE_TYPES` order: `{source, agent, installed}`. `source` and `agent` are as on `SessionRow`; `installed` is whether the agent's launch binary is on `PATH` (`shutil.which`). It feeds the client's agent picker for `rl handoff`. |
| `home` | `Home` | The inbox home screen's data; see `home` below. |
| `sessions` | `SessionRow[]` | See below. |

Membership is `contract.inbox_threads`: `overview.triage_inbox` over `home.state.visible_threads`
(filtered sessions excluded, woke threads included), sorted by parsed `mtime` (a naive time is
treated as UTC) then by id, oldest first. Splitting into sections, ordering within a split and
project grouping belong to the client; the rule that decides which side a row falls on is
`in_recent` and `woke`.

`SessionRow` keys (alphabetical in `contract.SESSION_ROW_KEYS`):

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
| `file_fingerprint` | string or null | `fileFingerprint`; the stamp the guarded verbs revalidate against. |
| `running` | boolean or null | `null` when presence is unknown. |
| `in_recent` | boolean | The unrounded age is at most `recent_days`. `age_days` is rounded to one decimal, so do not recompute this from `age_days`. |
| `woke` | boolean | `home.state.is_woke`: a snooze that has expired. |
| `lifecycle_block` | string or null | Why archive, and harvest (whose save step archives), cannot touch the transcript (the message of the `RuntimeError`, `ValueError` or `KeyError` raised by `scanner.filesystem_path(thread, "archive")`; for a `KeyError` that is the quoted key, not a sentence), else `null`. It is not a delete pre-check: a guarded `delete` can still refuse a row whose block is `null`, for example when the transcript or a companion lies outside its source's session store. |
| `badges` | string[] | `resume.badges`: any of `filtered`, `snoozed`, `kept`, `finishing`, `harvested`, `abandoned`, `drifted`. The set is closed: a new value is a contract change, pinned by `tests/test_contract.py`. |

The 500-character clip (`contract.clip`, `LIST_TEXT_MAX_CHARS`) keeps the list small: text longer
than the limit is cut to the first 499 characters plus `…`; `None` stays `None`. The full text
lives in the tail.

### `home`

The `home` key of the list: the data behind the TUI's Home and Projects screens, computed by the
same functions (`home.state.build_home_state` and `home.state.next_steps`) so the two UIs cannot
drift. It carries structure only, never display text: the client writes its own copy.

| Key | Type | Meaning |
|---|---|---|
| `counts` | object | `triage_total` (rows in `sessions`), `live`, `unclassified`, `blocked`, `stale`, `kept` (from `overview.compute_metrics` over the visible threads), `projects` (length of `projects` below) and `spread_projects` (projects with live sessions). |
| `pending_harvests` | number | Harvest files waiting to be published (`harvests.pending_count`, one per `*.md` file). Unlike the TUI's harvest view it does not parse them, so an unreadable file is counted too. |
| `projects` | `ProjectRef[]` | The triage queue: projects with sessions in `sessions`, oldest activity first (`home.state.project_queue`). `ProjectRef` is `{key, label, pending, unclassified, oldest_mtime, newest_mtime}`; `key` equals `SessionRow.project_key`. |
| `waking_soon` | string[] | Ids of snoozed sessions whose snooze ends within 7 days, soonest first. |
| `in_flight` | string[] | Ids of up to 8 sessions active in the last 7 days (live status or unclassified, not finishing, not snoozed), newest first. |
| `next_steps` | `NextStep[]` | The recommended next steps, in order. |
| `rows` | `SessionRow[]` | A row for every id in `waking_soon` or `in_flight` that is not in `sessions` (a snoozed session never is), in that order. |

`NextStep` is `{kind, deletes, costs_model, count, project_key, id}`:

| `kind` | When | `count` | `project_key` | `id` |
|---|---|---|---|---|
| `roundup` | the roundup is stale (see the list's `roundup` key) | `null` | `null` | `null` |
| `woke` | snoozes have expired | woke rows | `null` | `null` |
| `recent` | rows from the last `recent_days` | those rows | `null` | `null` |
| `publish` | `pending_harvests` > 0 | `pending_harvests` | `null` | `null` |
| `project` | a backlog beyond the woke and recent rows | the project's `pending` | first queue key | `null` |
| `resume` | only when nothing else applies and `in_flight` is not empty | `null` | `null` | `in_flight[0]` |

`deletes` and `costs_model` are `true` only for `roundup`. The set of kinds is closed: a new kind
is a contract change.

### `scan`

The `scan` key of a list produced by `rl inbox --json --scan`: `{scanned_at: string, added: number,
removed: number, unavailable_sources: string[]}`. `scanned_at` is ISO 8601 UTC; `added` and
`removed` are counts of set differences of thread identities before and after the refresh;
`unavailable_sources` is sorted. The rows in `sessions` are read from the refreshed registry. It is
not a document kind of its own.

### `tails`

The output of `rl data export --json`: `{version, kind: "tails", items: TailItem[]}`. `TailItem`:

| Key | Type | Meaning |
|---|---|---|
| `id` | string | The requested id. |
| `text` | string | `export.render_tail(thread, max_chars)`; `""` when unavailable. |
| `max_chars` | number | The limit applied. |
| `truncated` | boolean | `len(text) >= max_chars`. |
| `unavailable` | string or null | `"no longer in the registry"`, `"ambiguous session id"`, `"transcript not found"` or `"could not read the transcript (<ExceptionType>)"`. |
| `mtime` | string or null | The thread's `mtime` when the tail was read. |
| `file_fingerprint` | string or null | The thread's `fileFingerprint` when the tail was read. |

### `outcome`

The output of a guarded session verb with `--json`.

| Key | Type | Meaning |
|---|---|---|
| `version`, `kind` | `1`, `"outcome"` | |
| `action` | string | The decision applied: `archive`, `delete`, `harvest`, `snooze`, `keep` or `keep_going` (`mark reviewed`). |
| `id` | string | The requested ID. |
| `status` | string | `ok`, `refused`, `empty` or `failed`. |
| `reason` | string or null | For `refused`: the first refusal reason. Otherwise `null`. |
| `code` | string or null | For `refused` and `failed`: a fixed code (see Outcome codes). `null` for `ok` and `empty`. |
| `message` | string | Human-readable. |
| `wrote` | boolean | Something changed on disk or in the registry (true even for a partial failure). |
| `path` | string or null | Archive destination or saved harvest path. |
| `warnings` | string[] | Non-fatal problems (for example companion files that could not be removed). |

### `handoff`

The output of `rl handoff triage --dry-run --json`.

| Key | Type | Meaning |
|---|---|---|
| `version`, `kind` | `1`, `"handoff"` | |
| `purpose` | `"triage"` | What the agent is handed. |
| `status` | string | `ok` or `refused`. |
| `code` | string or null | For `refused`, a fixed code (`handoff.HANDOFF_CODES`, pinned by `tests/test_contract.py`): `nothing_to_triage` (no waiting project has that key, or that label uniquely) or `agent_not_installed` (the launch binary is not on `PATH`). Otherwise `null`. |
| `reason` | string or null | For `refused`, prose for a terminal; read `code`. Otherwise `null`. |
| `source`, `agent` | string | As on `SessionRow` and `agents[]`: the source name (`claude-code`) and the display name (`claude`), whichever form was passed. |
| `project_key` | string | The resolved key when `ok`; the value passed to `--project` when refused. |
| `cwd` | string or null | Where the agent would start: the newest member session's git root or cwd that still exists, else `$HOME` (`home.intents.handoff_cwd`). `null` when refused. |
| `argv` | string[] or null | The agent's launch argv with the triage prompt (`home.intents.triage_prompt`). `null` when refused. |

A refusal exits 0 like any printed document. Without `--json`, a refusal prints
`rl-handoff: <message>` on stderr and exits 1, and `--dry-run` prints the directory and argv for
humans.

`error` documents are `{version: 1, kind: "error", message: string}`.

### Outcome codes

`reason` and `message` are prose for a terminal; a client that writes its own copy reads `code`.
The set is closed (`codes.OUTCOME_CODES`, pinned by `tests/test_contract.py`); a new code is a
contract change.

| `code` | Status | When |
|---|---|---|
| `changed` | refused | `--if-mtime` differs from the registry, or the transcript's size or mtime on disk differs from `--if-fingerprint` |
| `no_fingerprint` | refused | `archive`, `delete` or `harvest` without a valid five-field `--if-fingerprint` |
| `gone` | refused, failed | the session left the registry (refused by a revalidated op, failed by `keep`, `keep_going`, `snooze`) |
| `transcript_missing` | refused | the stamped transcript is not on disk |
| `running` | refused | presence reports the session running |
| `presence_unknown` | refused | presence could not answer, so the session is treated as running |
| `lifecycle_blocked` | refused | the source has no filesystem lifecycle (OpenCode) or the thread's native roots are corrupt; `reason` is the row's `lifecycle_block` text |
| `until_invalid` | refused | `--until` is not an ISO 8601 time |
| `until_naive` | refused | `--until` has no timezone offset |
| `until_past` | refused | `--until` is not in the future |
| `no_harvest_model` | refused | `harvest` with no harvest model configured |
| `harvest_failed` | failed | the harvest model call failed |
| `failed` | failed | anything else that did not complete, including a partial write (`wrote` says whether something changed) |
| `other` | refused | free text no code covers, for example an `OSError` while checking the transcript, or a delete of a transcript outside its source's store |

Every code is set where the refusal or failure is produced: `revalidate.changed_refusal` and
`running_refusal` return a `Refusal(code, text)`, `decisions.Outcome` carries it as `code`, and the
guarded gates attach their own. Nothing is recovered from the prose, so rewording a message
never changes a code.

## Guarded decisions

Each guarded verb maps to one decision (`action` in the outcome document):

| Verb | `action` | Operation |
|---|---|---|
| `archive` | `archive` | `ArchiveOp` |
| `delete` | `delete` | `DeleteOp` |
| `harvest --archive` | `harvest` | gate, model, `HarvestSaveOp` |
| `snooze` | `snooze` | `SnoozeOp` |
| `keep` | `keep` | `KeepOp` |
| `mark reviewed` | `keep_going` | `KeepGoingOp` |

`--if-mtime` is the `mtime` the user saw. `harvest --archive` saves the harvest and archives the
session; it is not `rl session complete`, which has its own flow.

Status mapping (`contract.from_outcome`): `decisions.Outcome.ok` gives `ok`; not ok with a
non-empty `refused` gives `refused` with the first reason; any other not-ok gives `failed`. `empty`
is produced only by harvest.

| Action | Revalidated | Presence checked |
|---|---|---|
| `archive`, `delete`, `harvest` | yes, stamp required | yes |
| `snooze`, `keep`, `keep_going` | no | no |

- **Revalidation** (`revalidate.changed_reason`) compares `--if-mtime` with the registry thread's
  `mtime` and, when `--if-fingerprint` carries a content stamp, the transcript's size and mtime
  in nanoseconds on disk. A mismatch is a refusal with reason `changed since you looked`; a
  session that left the registry is refused with `no longer in the registry`. When a stamp is
  given, the disk check can also refuse with `transcript is missing`, the `RuntimeError` text of
  `scanner.filesystem_path`, or an `OSError` message, so clients should treat the reason as
  free text and read `code`.
- **Fingerprint requirement.** `archive`, `delete` and `harvest` are refused with
  `revalidate.NO_STAMP` (`changed since you looked: no fingerprint, rescan`) when
  `--if-fingerprint` is missing or is not a five-field numeric fingerprint, before the registry
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
  lock around any guarded operation: `stateio.publish_lock` opens a new file descriptor and takes
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
- **SIGTERM.** `contract_io.run_guarded` installs a handler that exits 143, and its `finally` kills
  any model child processes (`models.proc.kill_all_children`, only when `guarded` was loaded) and
  restores the previous handler even if the kill raises, so cancelling a harvest from the app does
  not leave a model running. SIGTERM is blocked
  (`stateio.sigterm_deferred`) for the whole archive transaction in `decisions.execute_group`,
  and from the harvest save through the commit in harvest, so a signal there exits only after the
  archive is recorded, with no outcome printed.
- **Snooze.** `--until` must be ISO 8601 with a timezone offset and later than now; it is
  stored normalized to UTC (`contract.normalize_until`, `astimezone(UTC).isoformat()`) because `overview.snooze_active` compares ISO strings (`now_iso < until`), which orders correctly only when both sides are in the same zone; do not keep the user's offset. Failures are refusals: unparsable input gives
  `--until is not an ISO 8601 time: …`, a naive time `--until needs a timezone offset`, a past
  time `snooze time must be in the future`.
- **Missing session on a non-revalidated op.** `keep`, `keep_going` and `snooze` do not
  revalidate; if the session has left the registry, `decisions.execute` turns the `KeyError` into
  a not-ok outcome with no refusal, which is reported as `failed` (message `… no longer in the
  registry`). It is not special-cased.
- **Mark reviewed.** The guarded `mark reviewed` records `--if-mtime` as the reviewed mtime. The
  plain `rl session mark reviewed` records the live file mtime.
- A decision is never retried automatically by `rl`.

## What the commands never do

None of them runs a scan except `rl inbox --json --scan`. None of them sweeps, prunes, enriches or
runs roundup. The list, export and handoff modes never write; `--scan` writes only the refreshed
registry.

## Design decisions

- **One process per call, no daemon.** The client spawns `rl …` each time; startup cost is kept
  down by lazy imports (see Startup cost). A persistent server speaking the same documents over
  stdin/stdout is the next lever if cold spawns still feel slow; it is not built.
- **The guarded path is selected by `--json` or `--if-*`; plain invocations are unchanged.** A
  session verb with neither behaves exactly as before: no revalidation, no presence check, no new
  output. Existing scripts and skills that call the plain verbs depend on that, and routing them
  through the guarded decisions would change what they refuse.
- **`--json` never changes a verb's effect.** It changes only how the outcome is printed. The
  guard (`--if-mtime`, `--if-fingerprint`) is what changes the decision, and a guarded call
  without `--json` does the same work and prints for humans.
- **Harvest needs `--archive`.** The guarded harvest saves and archives in one decision, so the
  flag makes that explicit and keeps it distinct from plain `rl session harvest`, which only
  saves.
- **Same ops as the TUI.** The guarded verbs run the `decisions.py` ops, so revalidation, presence
  and locks are not reimplemented. The only stricter rule is the required fingerprint for
  `archive`, `delete` and `harvest`.
- **No delete grace here.** A guarded `delete` acts immediately; the client owns the undo window
  (10 s, the TUI's `DELETE_GRACE_SECONDS`).
- **The client keeps at most one decision per session in flight** and never retries; `rl` has no
  outer lock to serialize them (see Locks).
- **The list is all or nothing.** A registry row without a string `mtime` makes `rl inbox --json`
  exit 1 with an `error` document, as the TUI's state loading fails on the same row; ordering is
  defined by `mtime`, so there is no per-row skip.
- **`lifecycle_block` is the archive/harvest lifecycle only.** Changing its shape, or adding a
  delete pre-check field, is a contract change (version bump on both sides plus goldens). A
  guarded `delete` stays the guard for delete refusals.

## Versioning

`version` is `1`. Adding a key to a document, or a new `unavailable`/`reason` string, is a
compatible change only if the client already ignores unknown keys; renaming, removing or
changing the type of a key, or changing the meaning of a status, requires `version: 2` and a
client that checks it. Key sets are pinned by `contract.SESSION_ROW_KEYS` and the golden
documents. Change the contract and the circus client (types, Rust envelope, goldens) together.

The `agents` and `home` keys on the list document, the `handoff` document, `code` on the outcome
document and `presence.code` on the list were added as compatible changes.

The `scan` key on the list document was added as a compatible change, and the standalone `scan`
document kind was removed when the machine interface moved onto the existing commands; clients
read the rescan result from the list's `scan` key and treat a missing or `null` value as no scan.

## Golden documents

`tests/golden/contract-list.json`, `contract-tails.json`, `contract-outcome-ok.json`,
`contract-outcome-refused.json` and `contract-handoff.json` are produced in-process by `tests/test_contract_golden.py` from a
deterministic fixture (`threads=20`, `transcripts=False`) with the temporary root replaced by
`/fixture` (also inside the Claude project-directory slug). In that fixture the transcripts do
not exist, so found tail items are `unavailable: "transcript not found"`. The list's `agents` is
pinned to Claude Code installed and every other agent not installed. Regenerate with:

```sh
RINGLEADER_UPDATE_GOLDEN=1 uv run python -m pytest tests/test_contract_golden.py
```

The circus repository keeps byte-identical copies (`just sync-golden` copies, `just
golden-check` compares), so change a golden only together with the contract and the client.
Each GitHub release also attaches them as `contract-golden.tar.gz`, flat, so a client pinned to
a release can check against exactly that release's documents.

## Synthetic world

`python -m ringleader.devtools.gui_world --root DIR [--threads N] [--exports]` builds a
disposable world for
development and QA of the app: a `tui_fixture` registry with real Claude transcripts, a
`claude-settings.json`, and `bin/` stubs (`claude` exits 97, `fake-harvest` answers harvests,
empty for any project directory ending in `/proj-00`), plus a fake `hooklinesinker` that reports
one running session. Its config points `ai_command_prefix` at `fake-harvest`. It refuses to build
inside `~/.local/share`, `~/.claude`, `~/.codex`, `~/.factory`, `~/.pi` or `~/.cursor`, and refuses to replace
any existing directory without a `.gui-world` marker, and any path that is not a directory. `--exports` prints `export KEY=value` lines
for every agent's store override and the XDG data variables, `RINGLEADER_CLAUDE_SETTINGS`, and a `PATH` that puts `bin/` first.
Source them (and `unset OPENCODE_DB`) before running `rl`; `--registry` alone is not isolation.
The module ships in the wheel with `devtools/tui_fixture.py`, so a venv holding only a release
wheel runs it with that venv's `python`.

## Testing

`tests/test_contract.py` (documents), `tests/test_json_commands.py` (the commands end to end),
`tests/test_contract_perf.py` (imports and timing), `tests/test_contract_golden.py` (goldens),
`tests/test_rl_handoff.py` (`rl handoff triage`) and
`tests/test_gui_world.py` (the world script).
