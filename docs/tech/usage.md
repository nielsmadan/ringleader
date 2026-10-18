# Usage analytics

`ringleader/usage/`: reading token spend out of five agents' local state, plus
subscription-window state.

## Purpose

Answer "where did my tokens go" without an API call, by parsing what each agent already wrote to
disk. Five readers normalize into one `UsageRecord` stream that the aggregation and rendering
layers treat identically.

## How it works

### The record

`UsageRecord` (`base.py`) is the normalized unit: source, session id, project, model, timestamp,
and the four token counts — `input`, `cache_read`, `cache_write`, `output`. They are kept
separate rather than summed because cache reads and writes price very differently;
`total_of()` combines them when a single number is wanted.
`reasoning` is also exposed as a breakdown field, but normalized as a subset of `output` so it is
never added twice. OpenCode stores reasoning separately from visible output, so its reader folds
the two together when constructing `UsageRecord.output`.

`UsageReader` is the protocol each harness implements. `aggregate.READERS` maps a source name to
its reader, and is the authority for `--source`'s valid values.

### Readers

| Module | Reads |
|---|---|
| `claude.py` | `~/.claude/projects/*.jsonl` usage records |
| `codex.py` | `~/.codex/sessions/**/*.jsonl` |
| `droid.py` | `~/.factory/sessions/*/<uuid>.settings.json` sidecars, not the transcripts |
| `opencode.py` | `$XDG_DATA_HOME/opencode/opencode.db` in SQLite read-only mode |
| `pi.py` | `~/.pi/agent/sessions/**` |

### Aggregation

`rollup(records, key)` buckets a record stream by any key function into `Bucket` totals;
`base.record_day()` and the `_KEYS` map in `commands/usage.py` supply the `--by day|model|project|
session|source` options. `in_window()` applies `--since` / `--until`, which argparse parses into
`date`s at the CLI edge (anything but `YYYY-MM-DD` exits 2); readers' `records(since=...)` take
that `date`, never a string.

`pricing.py` maps a model string to a family (`model_family()`) and estimates cost
(`estimate()`). Prices are a hardcoded table — deliberately, so the tool needs no network — and
therefore go stale when a provider reprices. An unrecognized model yields `None` rather than a
wrong number.

`ROUTED_PROVIDERS` is the short-circuit before the table is consulted at all: a provider that
routes requests and bills its own units cannot be priced from the model vendor's list price.
`factory` is the only member, so every droid record reports no cost.

### Droid, and its two approximations

`droid.py` is the one reader whose numbers are not in the transcript. Each session's
`<uuid>.settings.json` sidecar holds the totals, so the reader emits **one record per session**,
stamped at the transcript's mtime. Two consequences are worth stating rather than hiding:

- **`--by day` mixes granularities.** A long droid session's whole spend lands on the day it was
  last touched, beside per-turn records from every other source.
- **`--by model` attributes a whole session to one model.** Droid transcripts carry no per-message
  model, so an auto-routed session is credited to whichever model `effectiveFactoryRouterModel`
  last recorded.

Every droid thread reports its **own** `tokenUsage`, subagents included. The sidecar also carries
`inclusiveTokenUsage` and `childInclusiveTokenUsageBySessionId`, which would let one top-level
session stand for its whole subtree, but that attribution depends on which threads happen to share
a store. `rl session archive` moves a parent into `archive/` and leaves its subagents in
`~/.factory/sessions`, so `rl usage --archived` would then count a subagent once on its own and
again inside its parent's inclusive figure. Own-usage sums to the same total by construction and
does not shift when a session is archived, deleted or pruned.

### Subscription windows

`WindowSnapshot` carries a window's label, percentage used, and reset time.

- **Codex** writes its window state to disk, so `collect_windows()` just reads it.
- **Claude Code** does not. `claude_limits.py` reads a capture file written by a `statusLine`
  hook that `rl install statusline` wires in — Claude Code hands the status line a JSON payload
  containing the limits, and the snippet saves it.

`_WINDOWS` maps `five_hour` → `("5h", 300 minutes)` and `seven_day` → `("weekly", 10080)`.
`_EPOCH_LEAK_FLOOR` guards a real upstream quirk: a field documented as a percentage sometimes
carries a `resets_at` epoch instead, so any "percentage" above 1000 is reinterpreted as a
timestamp.

### Archived usage

`collect_archived()` in `commands/usage.py` reads the same `UsageRecord` stream out of
`archive_root()` (`~/.local/share/ringleader/archive/<slug>/*.jsonl`). It dispatches
through `aggregate.READERS` so every archivable source is included, and respects
`--source` filtering; each reader only emits records matching its own JSON shape
(`"token_count"` events vs `"assistant"` messages), so a codex `**/*.jsonl` glob
matching Claude's `*/*.jsonl` paths still produces no double-counting. `rl usage
--archived` merges that stream with live records and then `dedupe()`s. Claude, Codex and Droid
produce archivable transcripts today — OpenCode and Pi use separate stores. Droid's archived
figures still come from its `.settings.json` sidecars, which archiving carries along as
companion files.

### Cross-machine merge

`transfer.py` handles multi-machine totals. `write_export()` dumps records tagged with a
`machine_id()`; `load_exports(dir)` reads a directory of them; `dedupe()` drops records seen
twice. `rl usage --merge <dir>` is the consumer.

## Key entry points

- `usage/base.py` — `UsageRecord`, `WindowSnapshot`, `UsageReader`.
- `usage/aggregate.py` — `READERS`, `rollup()`, `in_window()`.
- `usage/pricing.py` — `PRICES`, `estimate()`.
- `usage/claude_limits.py` — `read_windows()`, `default_capture_path()`.
- `commands/usage.py` — `cmd_usage()` and its three sub-dispatches.

## Gotchas

- **Claude windows need `rl install statusline` first**, and only populate after the first API
  response in a session. Before that, `rl usage windows` reports nothing for Claude and says so.
  Pro/Max only, Claude Code v2.1.80+.
- **A percentage over 1000 is an epoch**, not a percentage — see `_EPOCH_LEAK_FLOOR`.
- **`PRICES` is a static table.** Cost figures are estimates and drift with provider pricing.
- **`--days` defaults to 7**, so a bare `rl usage` is a week, not all time. `--all` widens it.
- **Reading walks local session stores**, so `--all` over a large history is I/O-bound in the
  same way a scan is. The default window is not: `latest_day()` finds the newest record by
  reading files newest-mtime first (`base.newest_first()` yields each file with the latest
  instant a record in it can carry), and `records(since=...)` on the same reader then skips every
  transcript last written more than a day before the window start (a file holds no record newer
  than its last write). The window is resolved once, from `latest_day()`; only `--archived` and
  `--merge` anchor on the pooled records instead, and they still read everything, because
  deduping needs the full set. OpenCode filters on `time_created` in SQL. Droid reads every
  transcript header and sidecar once per reader, because its per-session dedupe keeps the first
  path in sorted order and an mtime prefilter would hand a twice-filed session to its newer copy.
- **`rl usage session <id>` must agree with `--by session`.** Claude records are deduped by
  (message id, requestId), first file in sorted path order wins, and a resumed or forked session
  copies its parent's lines with the same ids. So for Claude the command reads every transcript in
  the target's own project directory (where resumes and forks land) up to the target, and keeps
  only the target's records. A copy filed under a different project directory is not seen. The
  other readers open only files whose name carries the id prefix.
- **`rl usage windows` reads Codex rollouts newest first.** It stops once both windows have their
  latest snapshot, or, while one kind is still missing, once the files are more than a week older
  than the newest snapshot found. A kind last seen over a week before the newest snapshot is
  therefore not reported, even as expired.
- **Droid records are never priced**, and `rl usage windows` does not cover droid. Factory bills
  credits, and its window state is API-only — deliberately deferred, not overlooked.
- **The droid reader skips `sessions/btw/`**, droid's fork layout, exactly as the Source does. A
  fork filed there is never re-filed under a launch-dir slug, so its tokens are left out for good
  rather than counted later. Droid's third layout, `sessions/<uuid>.jsonl` at the global root, is
  read like any project directory.
- **OpenCode usage comes from assistant message rows.** The reader emits one immutable record per
  response so date windows and model grouping reflect when each response happened. It joins the
  owning session's opaque `project_id` to `project.worktree`, and includes child/sub-agent rows
  because their spend is separate from the parent. The curation Source excludes those rows because
  they are not user-owned resumable sessions. The reader never derives a project id from a path or
  writes the database.

## Why

Parsing local agent state rather than querying a billing API: it needs no credential, works
offline, covers agents that expose no API at all, and keeps working when a provider changes its
reporting surface. The cost is that pricing is a table we maintain.

## Related

- [features/usage-analytics.md](../features/usage-analytics.md)
- [reference/agent-session-management.md](../reference/agent-session-management.md) — where each
  agent puts its transcripts
