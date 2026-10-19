# Session analysis

`ringleader/analysis/`, `commands/analyze.py`: how `rl analyze` turns agent transcripts into a
SQLite index and reads signals, health, rollups, recurring issues and labels back out of it. For
what the user sees, see [features/analysis.md](../features/analysis.md). The decision to use a
derivable SQLite cache is [ADR 0015](../decisions/0015-sqlite-analysis-index.md).

## Purpose

Answer "where did my sessions go wrong, and where did the tokens go" over every tool call,
request, user message and compaction on disk. Per-call rows and grouped queries do not fit the
JSON-file pattern the rest of ringleader uses, so the facts live in an index that can always be
rebuilt from the transcripts.

## Module map

| Module | Owns |
|---|---|
| `facts.py` | The fact dataclasses (`TranscriptFacts`, `RequestFact`, `ToolCallFact`, `UserMessageFact`, `CompactionFact`, `RewindFact`), the `ErrorCategory` type and the capture caps. |
| `adapters.py` | One `AnalysisAdapter` per indexed agent: live root and globs, archive companion globs, the first-record sniff, the extractor and a post-ingest hook. The archive globs and the tokens-only sources derive from this list. |
| `build.py` | The fact builders both extractors share: `request_fact` (gap and rebuild rule), `call_tail` (MCP parts, command class, signature) and `message_fact` (cap, rule kind, hash). |
| `claude.py`, `codex.py` | One lenient pass over a transcript, producing `TranscriptFacts`. Claude walks the message graph for the live branch and rewinds. Codex skips fork replay and filters by own thread. |
| `taxonomy.py` | Tool kinds, error categories and their patterns, command classes, benign-exit rules, validation patterns. Everything that shapes a stored fact. |
| `signals.py` | The 14 signal detectors over one thread's live calls. |
| `episodes.py` | Merges overlapping hits into episodes, splits them on gaps, ranks by severity. |
| `health.py` | Outcome classification, penalties, the 0–100 score and points per 100 calls. |
| `store.py` | DDL, the two version constants and their fingerprints, connections, the write lock, `replace_transcript`, `rederive`, the labels DDL. |
| `ingest.py` | Discovery, the archive sniff, the refresh loop, locking and the read-without-refresh paths. |
| `scoping.py` | The scope filter (agents, archived, project selectors) and the day clause, shared by rollups and labels. |
| `rollups.py` | Query-time dedupe, totals, cache stats, the eight dimensions, worst sessions, session detail. It returns dataclasses, never JSON. |
| `issues.py`, `embed.py` | Recurring error signatures, and the optional embedding merge. |
| `labels.py` | Label prompts, the plan and cost estimate, the labels store reads and writes. |
| `render.py` | Every JSON document and block (corpus, session, issues, labels, reports) and the text views, which are drawn from the same dicts. The text views strip terminal control characters from transcript-derived text. |
| `reports.py` | The saved findings and recommendations files. |

## Data flow

1. `ingest.discover()` lists transcripts under `~/.claude/projects`, the Codex sessions dir and
   the archive, with a stat fingerprint each.
2. `ingest.refresh()` compares fingerprints with the index, extracts only the changed
   transcripts and drops the vanished ones.
3. Each extracted transcript goes through `store.replace_transcript()`: the fact rows plus the
   derived rows (`signals`, `episodes`, `thread_stats`) in one savepoint.
4. `rollups`, `issues`, `labels` and `render` open the index read-only and answer the command.

`commands/analyze.py` is the only writer of the index. `scripts/benchmark_analyze.py` reads it
too, through a private copy (see [the benchmark](#optional-extras-and-the-benchmark)).

## Constraints on changes

- **Pure core, I/O at the edges.** Extractors, signals, episodes, health, issue normalization
  and rendering take data and return data. Only `ingest.py`, `store.py`, `reports.py` and
  `commands/analyze.py` touch disk. `now`, clocks and presence are passed in as parameters.
- **Every `sqlite3.Connection` is closed with `contextlib.closing`.** An unclosed one raises
  `ResourceWarning`, and pytest runs with `filterwarnings = error`.
- **Tests never read `~/`.** They build synthetic transcripts under `tmp_path`, and conftest
  isolates the agent environment, guards the live state dir and blocks real model CLIs.
- **The heuristics are borrowed, not the code.** Several detectors follow agentsview and
  sniffly, but ringleader takes no dependency on either.
- **Parsing uses stdlib `json`.** orjson is worth adding only if a cold build of a real corpus
  goes past five minutes. The last measured `--all` cold build was 11,727 transcripts in 173 s.
- **Signals stay deterministic.** Model labels add names and causes on top of them. They never
  change a score, an episode or a rollup.

## The index

`analysis/index-v{FACTS_VERSION}.sqlite` under the state dir, or under `--index DIR`. The filename
carries the facts version, so a newer `rl` and an older one never write the same file.

### Tables

Facts, rebuilt only by re-extracting a transcript:

- `transcripts`: one row per transcript file. Keys are `path` and `sort_key`; also root, agent,
  session and thread ids, fingerprint, cwd, first and last timestamp, `last_event`, `archived`.
- `requests`: one per model request, with the five token counts, the 5m/1h cache-write split,
  context size, gap since the previous request and the `rebuild` flag.
- `tool_calls`: one per call, with kind, command class, error category and signature, an input
  hash and a short preview. `on_main_branch` is false for calls on a rewound branch.
- `user_messages`, `compactions`, `rewinds`.
- `failures`: transcripts that could not be extracted, with the fingerprint and `facts_version`
  they failed at.
- `meta`: `facts_version`, `derive_version`, `created_at`, `complete_at`.

Derived, rebuilt from the facts alone:

- `signals`: `(path, kind, start_seq)` rows.
- `episodes`: one per merged stretch, with `content_hash` over its calls and an excerpt.
- `thread_stats`: calls, live calls, errors, writes and episode tokens per transcript.

Child tables cascade on `transcripts(path)`. `episodes(path)` has its own index because cascade
deletes otherwise scan the whole table. The facts indexes are created after a cold build, not
before, so the bulk insert stays fast.

### Query-time dedupe

The same request or call appears in more than one transcript: a resumed or forked Claude
session, a transcript that exists both live and archived, a subagent copied into a parent. The
index stores every copy. Every rollup collapses them at read time:

```sql
ROW_NUMBER() OVER (PARTITION BY dedupe_key ORDER BY t.sort_key, r.line) ... WHERE rn = 1
```

- `sort_key` is `str(path)`. It matches the order the Claude usage reader walks files in, so
  `rl analyze` and `rl usage` agree on which copy wins.
- The dedupe key is per table. Claude requests use `message id|request id`, calls use the
  `tool_use` id and messages use the record uuid. Codex keys include the rollout key and a line
  or sequence number, so two rollouts never collapse into each other.
- If the winning path of an episode id is dropped, the other copy's episode returns only after
  its own fingerprint changes or the derive version bumps. `--rebuild` restores it at once.

## Versions and the pinned-hash rule

Two integers decide what a run recomputes:

- **`FACTS_VERSION`** names the index file. A change to the facts DDL, the taxonomy or the
  capture caps (`COMMAND_CHARS`, `ERROR_HEAD_CHARS`, `MESSAGE_CHARS`, `REBUILD_READ_RATIO`, ...)
  needs a bump. The next run builds a new file from scratch and deletes older-version files,
  under the write lock. It never touches a newer file and never unlinks the live one.
- **`DERIVE_VERSION`** is stored in `meta`. A mismatch drops and recreates `signals`, `episodes`
  and `thread_stats` and refills them from the stored fact rows in one transaction, with no
  transcript I/O (`store.rederive`). Thresholds, penalties and episode splitting live here.

`store.facts_fingerprint()` and `store.derive_fingerprint()` hash the inputs of each version:
the DDL, the taxonomy, the caps and every threshold and penalty. `tests/analysis/test_versions.py`
pins each fingerprint to its version in `FACTS_PINS` and `DERIVE_PINS`. Editing any hashed input
fails the suite until you bump the version and add the new pin. This is deliberate: it is how a
threshold edit cannot leave an index holding signals from the old values.

The hash cannot see extractor logic. A change to what `extract_claude` or `extract_codex`
produces for the same transcript needs a hand-made `FACTS_VERSION` bump, with a pin.

## Failures

A transcript that raises during extraction or derivation is rolled back to its savepoint and
recorded in `failures`, and the run goes on. It is retried only when its fingerprint changes or
`FACTS_VERSION` moves. The error goes to the command's `notice` callback, which prints it to
stderr on a line of its own: on a terminal it first clears the `indexed N of M` progress line.

`index.failed` in the JSON counts every transcript currently recorded as failed, from any run.
`index.failed_now`, `attempted` and `changed` count this run only.

After a refresh, `rl analyze` exits 1 and prints a note when more than 20% of the changed
transcripts failed (`FAILURE_RATIO_LIMIT`), counted only once at least 20 were attempted. The
output is still produced.

## Locking, WAL and unavailable states

- Writers take `index.lock` with `flock`, non-blocking first. A writer that finds it held and
  an index that is complete and current returns that index with `stale` set, without waiting,
  and says on stderr that results may be partially refreshed: a warm refresh commits every 200
  transcripts, so a concurrent reader can see a mix of the old and the new build. Otherwise it prints a waiting notice and blocks. `--rebuild` always
  waits.
- The database runs in WAL mode with a 30 second busy timeout. Readers open it `mode=ro` with
  `query_only`.
- `complete_at` is set only when a build finishes. A cold build commits every 200 transcripts,
  so a crash leaves an index without it, and no reader treats that as usable.
- `--no-refresh` never writes. With no complete index it exits 3. With an index from another
  version it also exits 3 and names the mismatch, so a skill that treats 3 as "do not build here"
  never falls through to a rederive. When the index is not complete it tries a shared lock; if a
  writer holds it, it prints the waiting notice, waits for the writer (a blocking shared lock),
  then reads again.
- `store.IndexUnavailable` carries a `reason`, and the exit code follows from it: `missing` and
  `mismatch` exit 3 (`store.EXIT_NO_INDEX`), `unreadable` and `unwritable` exit 1. The `--json`
  error document reports it as `index.reason`.
- A state dir that cannot be written falls back to reading the existing index, reported as
  `writable: false`, as `not writable` on the text index line, and on stderr for every view.
  With no current index it exits 1.
- An index in a read-only directory without its `-shm` file cannot be opened safely, and is
  reported, not worked around. There is no `immutable=1` and no copy-to-temp fallback.
- An index file SQLite cannot read exits 1 and says to delete it. Deleting it costs a cold
  build and nothing else.
- `--index DIR` moves the index, the lock and `labels.sqlite` together. Saved reports and the
  Codex start cache stay under the state dir (see below).

## Discovery and archive dispatch

Every agent-specific rule lives in one `adapters.AnalysisAdapter` per indexed agent. Claude
transcripts match three globs under `~/.claude/projects`: main threads, subagents and workflow
subagents. Codex rollouts match `**/*.jsonl` under the Codex sessions dir. A subagent's
fingerprint also includes its `.meta.json`. The archive keeps each transcript beside its
companion files, so its globs are `*/*.jsonl` plus each adapter's companion globs one directory
down (`*/*/subagents/**/agent-*.jsonl` for Claude). Adding an agent means adding an adapter.

The archive holds files from several agents in one tree. `sniff_agent` reads at most 20 lines of
each and returns the agent from the first recognisable record: `session_meta` is Codex, a record
with `sessionId` and a Claude record type is Claude. A first record that names another agent's format
(`session_start`, `session`) is skipped. A recorded agent is reused while the fingerprint is unchanged, so only new
or changed archive files are sniffed again.

An unlistable root marks its transcripts as unavailable instead of vanished, so a missing mount
does not empty the index.

## Extraction

- **Whole file, always.** A transcript whose fingerprint changed is re-extracted from the start.
  A live session therefore costs one full read per run. The no-change run is cheap because
  nothing else is read. There is no append-only path yet (see the roadmap).
- **Claude.** The extractor builds the message graph and keeps the live branch. Calls and requests
  on abandoned branches stay in the index with `on_main_branch = 0`. Rewinds are recorded only on
  main threads, since subagent transcripts have none worth reporting.
- **Codex.** A forked or spawned rollout begins with its parent's history. The extractor skips
  every record before `usage.codex.own_events_start`, the same helper `rl usage` uses, and
  filters events by `own_thread` where a thread id exists. A subagent's `session_id` is the root
  thread, and a fixpoint `UPDATE` after each refresh rolls depth-2 spawns up to it.
- **Droid, OpenCode and Pi** have no extractor. They appear in totals and the `project`, `model`
  and `agent` dimensions through the usage readers, tokens only, never in calls, signals or
  episodes. `coverage` in the JSON says which. With `--archived`, archived Droid sessions are read
  from the archive and deduped with the live ones, as `rl usage --archived` does.
- **Shared parsing.** Token counts come from `usage.claude.claude_tokens` and
  `usage.codex.codex_tokens`, the same functions the `rl usage` readers call, so the 1h cache-write
  clamp and the cached-input split cannot drift between the two commands.
- **Codex response outputs** are parsed and clipped to head and tail when they arrive, so a rollout
  with thousands of shell calls does not hold every raw output until the end of the scan.

## Rollups

`rollups.corpus_rollups` reads the deduped requests and calls once and feeds the totals, the
cache stats and every requested dimension from that scan. The eight dimensions are `project`,
`folder`, `skill`, `plugin`, `mcp_server`, `tool`, `model` and `agent`. `mcp_server` and `tool` are
call-only: they have no token columns.

- **Window.** With no flag the window is the last `--days` days (default 7), ending at the latest
  day present for the chosen sources: the index rows of those agents (archived ones only with
  `--archived`) and the tokens-only readers. It does not end at today. `--all` removes it.
  `--until` alone has no start.
- **Streaming.** The scans iterate SQLite cursors instead of fetching every row, and the cache
  stats are folded into the request pass. SQL functions are registered before a scan starts,
  since SQLite cannot replace one while a statement is open.
- **Model filter.** Request rollups for Claude count only models whose name contains `claude`, the
  filter `rl usage` applies. `<synthetic>` and others are excluded in both. A non-Claude model run
  through Claude Code is invisible to both commands. This is what keeps their token totals equal.
- **Errors.** `errors` and the error rate exclude the not-agent categories: interruption,
  user rejection, permission denial and cancelled siblings. They appear only as their own user
  signals, never as the agent failing.
- **Compactions are per session.** An inferred compaction (a context drop in a transcript with no
  recorded one) is dated by the call it precedes, or by the transcript's last day, so a window
  counts only the compactions inside it. A dimension row reports the compactions of the sessions it
  touches. A session that used two models counts its compactions under both, so the column does
  not sum to the total. The totals row is exact.
- **Worst sessions** group threads into sessions. A session needs at least 5 calls. Points per
  100 calls divide by the whole session's stored `thread_stats`, not the day window's slice, and a
  session is listed if it has any activity in the window. Its health is computed over all of its
  threads by the same function `rl analyze session` uses, so the two always agree. The ranking is
  points per 100 calls, then episode tokens.
- **Day indexes.** A windowed scan reads requests and calls through a `MATERIALIZED` CTE filtered
  on `day`, so `requests_day` and `tool_calls_day` drive it instead of a full scan. Context windows
  are looked up only for transcripts with a request in the window; each one's peak still spans the
  whole transcript.
- **Context fill.** A request's fill is its context tokens over its thread's window: the window
  Codex records, else 1M for a Claude Code thread that peaked above 200k, else 200k. A recorded
  window of zero or less is unknown: its requests add no fill, and the session view reports the
  window as `null` (`unknown` in text) instead of dividing by it.
- **Two hit rates.** `cache.hit_rate` is computed from the indexed requests only, like the rest of
  `CacheStats`. `cache_hit_rate` on the totals and on each rollup row is computed from that row's
  tokens, which include the tokens-only agents. They differ whenever Droid, OpenCode or Pi records
  are in scope. The session document carries only `cache.hit_rate`.
- `RollupRow.requests` exists so `totals.requests` has a source. Project selectors resolve against
  the registry through `scoping.scope_filter`, which every scan builds from the scope and the
  project map; `error_calls` takes the project map as a required argument for the same reason.

### Rebuild cost with an unpriced model

`CacheStats.rebuild_cost` prices each model's rebuilt cache writes through
`usage.pricing.estimate`, with 1h writes at 2x the input price and 5m writes at the cache-write
price. A model with no price family contributes nothing, and `estimate` returns `None` for it:
that covers any model whose name is not Claude and any record from a routed provider. The sum is
over the priced models only, and it is `None` when no model was priced.

Unlike `tokens.cost_partial`, rebuild cost carries no partial flag. `rebuilds` and
`rebuild_tokens` still count every model, so a corpus with unpriced models shows a cost below
what the tokens imply. Read the cost as a floor.

## Health and outcomes

`health.health` takes per-kind signal counts, the thread's call and error totals and an outcome.
Each signal kind contributes `min(points x count, cap)` from `PENALTIES`, which is keyed by
`SignalKind` plus the three pseudo-signals; a test fails when a signal kind has no penalty. The error rate adds
`round(30 x errors / calls)` up to 15 once a thread has 5 calls. The score is
`max(0, 100 - points)`. Points per 100 calls is the ranking value. There are no letter grades.

`classify_outcome` is `open` when presence says the session is running, or when presence is
unavailable and the last event is under 10 minutes old (`RECENCY_GUARD`). Otherwise `api_error`
is `errored`, a trailing interrupt, abort, pending tool or user message is `abandoned`, and a
trailing assistant text or turn complete is `completed`.

Hits that share `(kind, start_seq)` collapse to one row, so a burst of interrupts at one spot is
one event.

## Recurring issues

`issues.find_issues` groups failed calls by `signature` (`command class|category|normalised first error line`)
across sessions. A signature seen in fewer than `--min-sessions` sessions (default 2) is dropped
before any merging. The ids are a hash of the sorted signatures, so they are stable.

With the `embed` extra, signatures are embedded with `minishlab/potion-base-8M` and merged above
a cosine similarity of 0.92, over at most 500 signatures. The embedder is optional and local
only. Without the extra, or without the model, exact signatures group and the JSON `embed` block
says why (`extra not installed`, `model not downloaded`). `embed.used` stays true when fewer than
two signatures remain, since there was nothing to merge. It turns false, with a reason, if
encoding fails or returns the wrong number of vectors.

The embed model loads with `HF_HUB_OFFLINE` set. `rl analyze issues --download-embed-model` lifts
that once, and is the only path that touches the network.

Not-agent categories still form issues. The category is the second `|` field of the
signature, not a field of its own.

## Names across layers

One user action has a different name in each layer. The code keeps these names; this table is
the mapping.

| Concept | Error category (`tool_calls.error_category`, signature field 2) | Signal kind | Elsewhere |
|---|---|---|---|
| The user interrupted | `interruption` | `user_interruption` | `user_messages.is_interrupt`; the session JSON's notable message kind `interrupt`, which is not a label `MessageKind` |
| The user declined a call | `user_rejected` | `rejected_tool_use` | the episode friction tag `user_rejected_action` |
| A permission or sandbox refusal | `permission_denied` | `permission_denial` | |

Call counts are `calls` and `errors` on rollup rows, `tool_usage` entries and `thread_stats`, and
`tool_calls` and `tool_errors` on a worst-session entry. A subagent entry in the session JSON uses
`tool_calls` and `errors`. All of them exclude the not-agent categories from the error count.

## Labels

Labels are model output, stored apart from the index in `labels.sqlite` next to it, so
rebuilding or deleting the index never loses them. The file has its own `schema_version` in
`meta`, and `LABELS_DDL` is pinned in `tests/analysis/test_versions.py`. Opening a file with no
version or an older one brings it to the current schema; a file a newer rl wrote is read but not
written, and an explicit `--label` then skips with a note. Inserts name their columns.

- `episode_labels (episode_id, content_hash, prompt_version) -> category, friction, cause`
- `message_labels (message_key, content_hash, prompt_version) -> kind`
- `issue_names (issue_id, content_hash, prompt_version) -> name, summary`

A row is current while the episode's `content_hash` and `LABEL_PROMPT_VERSION` match. Changing the
prompt or taxonomy bumps `LABEL_PROMPT_VERSION`, which makes every old row stale and re-billable.
Nothing deletes a stale or orphaned row: labels are never rebuilt and never garbage-collected. An
issue counts as named by `(issue_id, prompt_version)`, ignoring `content_hash`, so new examples
joining an issue do not pay for a rename.

`labels.plan_labels` decides the work before any model call and returns an estimate. Batches are 4
episodes, 75 messages, or 1 issue per call, with 2 attempts. A scope of `recent` limits episodes
to the last 7 days of the window and 50 episodes, messages to 500 and issues to 20. `all` lifts
all three caps. `rl analyze session <id>` labels that session's episodes without the cap.

An explicit `--label recent|all` that skips (no model, an unwritable or newer store), fails every
call or trips the breaker exits 1. Ctrl-C cancels in-flight calls: the CLI engines kill their
process, and the `llm` engine runs each request on a daemon thread that is abandoned, not joined.

The model comes from `models.active_model(cfg, "analyze")`: `analyze.model`, else `haiku` on the
`claude` engine, else the shared `model`. With no model, or an unwritable index dir, the run
continues with labels skipped and a note. Model text shown in the terminal is flattened and
capped (cause 140 characters, name 80, summary 240).

An episode's id is the hash of its first call, so it is stable while a transcript grows, except
when a later hit merges backwards into an earlier piece. That episode gets a new id and is labelled
again, and the old label stays orphaned. Finished sessions are what gets labelled, so this is rare.

## Reports

`reports.save_report` writes a Markdown file with front matter (`kind`, `id`, `created`, `title`,
`window`, `based_on`) to `analysis/findings/` or `analysis/recommendations/` under the state dir.
`--index` does not move them. The path is claimed with `O_EXCL`, so two writers never share a
name, and a failed write unlinks the claim. `based_on` ids must name existing reports. A body
with only front matter is refused.

Report text is data. A report is written by one agent and read by another, so it is an injection
path. The `ringleader-analyze` and `ringleader-recommend` skills and the playbook say that
report, transcript and tool-output text is never followed as instructions.

## Codex start cache

`usage.codex.StartCache` stores each forked rollout's `own_events_start` result in
`analysis/codex-own-starts.json`, keyed by path and stat fingerprint and versioned by
`REPLAY_VERSION` (2), which is pinned by a hash of the replay functions. `own_events_start`
streams the rollout once and stops at the recorded ordinal or the thread's own
`thread_settings_applied`, so a large fork is never held in memory. `rl usage` writes it. Each
`CodexUsageReader` loads one cache on first use (`starts=` injects one) and keeps it across
`latest_day` and `records`; the command flushes it once at the end (`aggregate.flush_caches`),
with a private (0600) atomic write. The analysis ingest only reads it, so `rl analyze --index`
never writes the live state dir. A forked
rollout `rl usage` has not seen is read again on each ingest. A change in a helper shared with
the replay code can leave stale starts until `REPLAY_VERSION` is bumped.

## Optional extras and the benchmark

- `uv sync --extra embed` installs `model2vec` in a source checkout, and
  `uv tool install --reinstall '.[embed]'` adds it to the installed `rl`; the "extra not installed"
  reason names the latter. Tests never import it or download a model.
- `scripts/benchmark_analyze.py --index` takes the index dir, as `rl analyze --index` does, or the
  index file itself.
- `scripts/benchmark_analyze.py` measures how well a cheap model labels episodes. It copies the
  live index with the SQLite backup API through a read-only connection and reads the copy. SQLite
  may leave empty `-wal` and `-shm` sidecars beside an idle WAL index, as any reader does; the
  database file itself is never written, and a test pins its hash. See
  [docs/tests/analyze](../tests/analyze/README.md).

## Performance

The measured numbers live in [Calibration](#calibration): a cold build of about 11,600
transcripts stays under the 5 minute bar that would justify a faster JSON parser, so none was
added, and a no-change run is seconds of ingest plus the rollup queries. They are one machine and
one corpus, not guarantees.

- **Where the time goes:** the cold build is JSON parsing and row inserts. A warm run is the
  rollup queries and, for `rl usage`, the Codex replay detection.

See [Rollup and replay-cache performance](#rollup-and-replay-cache-performance) for the numbers
before and after, and the roadmap for parallel extraction.

## Calibration

Calibration ran in two rounds on 2026-10-18. Round 1 tuned threshold and penalty constants and widened the taxonomy (`FACTS_VERSION` 3, `DERIVE_VERSION` 3). Its top-20 read found detector defects that constants could not fix, and those were fixed in round 2 (`FACTS_VERSION` 4, `DERIVE_VERSION` 4), and a third pass fixed what review of that round found (`FACTS_VERSION` 5, `DERIVE_VERSION` 5). The numbers below are round 2 unless a column says otherwise.

### Run

- **Date:** 2026-10-18, on `8a442ec` plus the round-1 and round-2 changes (committed as `8af3954`).
- **Corpus:** Claude Code 7,380 transcripts at the round-2 cold build: about 4,870 under `~/.claude/projects` (1,275 sessions) and 2,512 in the archive (261 sessions). That is 1,536 main threads and 5,844 subagent threads. Codex had about 4,200 rollouts plus 35 archived ones; it was tokens-only until the Codex extractor landed, so none of them were indexed in these rounds (see Codex calibration). The archive held no transcripts from other agents.
- **Isolation:** `XDG_DATA_HOME` and `--index` pointed into a temp dir that held a copy of the registry and symlinks to the archive and the opencode data dir. Only `rl analyze` and `rl usage` ran. Everything directly under the live state dir, the archive dir and an md5 of the archive listing were `stat`ed before and after each round, and the diff was empty both times. No `analysis/` dir appeared under it.

### Timings and size

| | v2 constants | v3 (round 1) | v4 (round 2) |
|---|---|---|---|
| Cold build, wall (`rl analyze --all --json`) | 161.6 s | 167.8 s | 155.1 s |
| of which ingest (`index.seconds`) | 97.0 s | 101.6 s | 88.2 s |
| No-change re-run, wall | 67.8 s | 64.5 s | 64.8–70.4 s |
| of which ingest | 2.2 s (6 live transcripts changed) | 2.2 s (4 changed) | 1.7–1.8 s (4 changed) |
| No-change, `--source claude` | 23.7 s | 24.6 s (ingest 0.4 s) | 25.8–29.4 s (ingest 0.4–2.7 s) |
| Default window (7 days), all sources / claude | | 14.0 s / 8.1 s | 15.3–16.9 s / 7.0–8.0 s |
| Index file | 543 MB | 537 MB | 536 MB |
| `index.failed` | 0 | 0 | 0 |

Round-2 ranges cover two runs of the same code. The cold build stays under the 5-minute bar, so no faster JSON parser was added.

The no-change run is not dominated by ingest:

- In an `--all` run, about 40 s goes to the Codex tokens-only read. `usage/codex.py` `own_events_start` re-reads rollout heads for fork-replay detection, about 26 s of that. `rl usage --source codex` pays the same cost.
- The per-dimension rollups re-scan and dedupe requests and tool calls once per dimension, plus once for totals. With `--all --source claude` that is 4 × about 4.2 s plus 4.7 s for totals, out of 25 s. Over the default 7-day window it is 4 × 1.2 s plus 1.4 s, out of 7.2 s. Most of it is `fetchall` on `SELECT r.*` with a window-function dedupe.
- Sniffing archive files that are neither Claude nor Codex costs nothing, because the archive holds none. Re-sniffing all 2,547 archive files would take 0.4 s, and the 35 archived Codex rollouts are re-sniffed each run in under 10 ms. No skip-marker table is needed.

Both costs were removed afterwards; see Rollup and replay-cache performance.

### Parity

| Window | `rl analyze` `totals.tokens.total` | `rl usage` `total_tokens` |
|---|---|---|
| Claude, 2026-01-01..2026-10-17 | 34,759,867,214 | 34,759,867,214 |
| Claude, `--all`, right after a refresh | 35,776,965,648 | 35,776,965,648 |
| Codex, 2026-01-01..2026-10-17 | 12,711,542,900 | 12,711,542,900 |
| Codex, `--all` | 13,418,709,499 | 13,418,709,499 |

The closed windows match in both rounds. An open `--all` comparison of Claude differs by about 0.7–1.1 M tokens whenever live sessions write turns between the index refresh and the `rl usage` read; a second round-2 run showed 1.0 M for that reason. With no refresh gap the totals match exactly.

### Error categories

On the v2 taxonomy, 1,275 of 6,467 error calls (19.7%) had no category. The large uncategorised groups were:

- Claude Code's own refusals: blocked `sleep` chains, and a worktree-isolated session reaching outside its tree.
- Subagent launch failures: a failed spawn, the concurrency limit, a fork inside a forked worker.
- Auto-mode classifier outages, which deny the call.
- "File does not exist", EISDIR and ENOENT.
- Reads over the token or size cap.
- Fetch refusals and HTTP 4xx/5xx errors.
- Cancelled parallel siblings.
- "Command failed (exit code: N)" from non-Bash shell tools.
- MCP input validation errors.
- Two permission phrasings the pattern missed: "this tool use was denied", and a multi-line "Permission to use Bash with command … denied".

Round 1 added `harness_blocked`, `sibling_cancelled`, `subagent_error`, `file_too_large`, `bad_path` and `fetch_failed`, and widened `permission_denied`, `user_rejected`, `input_validation` and `exit_code`. Uncategorised errors fell to 76 (1.2%). Classifier outages count as `permission_denied`. Like the existing auto-mode denial phrasing, they are a refused call rather than an agent failure.

Round 2 leaves the categories alone. It changes which exits are errors at all (see detector changes), and the store now holds 6,163 error calls, 77 of them uncategorised (1.2%). The trailing-search pipeline rule alone turned about 235 calls from errors into `ok` between two builds of the same corpus. The 39 `sibling_cancelled` calls stay errors with their category, but no longer count as agent failures.

### Detector changes (round 2)

- **search_loop** resets on a human message, ignores everything before a thread's first write, and counts shell file writes as writes. A shell call is a write when any segment redirects `>`/`>>` to a file other than `/dev/null`, or runs `tee` on one. Heredoc bodies are stripped before a command is split, so their contents are never segments. `python - <<…` and other interpreter-side writes are not detected. `1>`, `&>`, `&>>` and `>|` count as file redirects; `/dev/null`, `/dev/stderr`, `/dev/stdout` and `/dev/tty` do not. `<<` inside a quoted string does not open a heredoc. `2> file` is still treated as stderr capture, not a write, because `echo 2 > f` cannot be told apart after tokenising. 10,702 Bash calls (and 75 `Execute` calls) are now kind `write`, most of them `cat > f <<EOF` edits and output captured to scratch files.
- **reread_churn** counts only reads that overlap the current read: the same `input_hash`, or an overlapping line range of the same file. The range is `[offset, offset + limit)`, with offset 0 and limit `DEFAULT_READ_LIMIT` when absent, so a whole-file read overlaps any page inside its first 2,000 lines. PDF reads (`PAGED_SUFFIXES`) are paged by a `pages` parameter that is not stored, so they overlap only when the `input_hash` is identical. The facts now carry `read_offset` and `read_limit`.
- **Benign exits:** a compound command whose last search exits 1 is `ok`, with or without output. The last search is the final segment if it is `grep`/`egrep`/`fgrep`/`rg`/`git grep`, or the search that the final pipeline's trailing filters read from (`head`, `tail`, `sort`, `uniq`, `wc`, `cut`, `tr`, `column`, `nl`). The pipeline case is there because the harness shell evidently runs with pipefail: `grep … | head` exits 1 when grep matches nothing. A search joined by `&&` stays an error, since the exit may come from the command before it. A search fed by a pipe (`X | grep …`) is `ok` only when the call printed nothing, because under pipefail output means the exit came from `X`. `Execute` failures (`Command failed (exit code: N)`) are parsed like Bash ones. A single `git grep` that exits 1 with no output is `ok`, like `grep`. The check reads the whole command, not the stored 500-character cap.
- **Not-agent failures:** `USER_CAUSED_CATEGORIES` became `NON_AGENT_FAILURE_CATEGORIES`, the user-caused categories plus `sibling_cancelled`. It feeds `tool_failure`, failure streaks, runaway windows, episode severity, `thread_stats.errors` and the rollup error counts. Rejection, denial and interruption signals still fire from their categories.
- **Subagent rewinds:** the extractor drops rewinds detected in subagent transcripts. Their off-branch calls stay off the live branch.
- **Unknown tools:** `Execute` is a shell tool, classified from its `command` input like Bash. `ApplyPatch` is a write tool, and its file path is the first `*** Update/Add/Delete File:` line of its `input`. Both shapes come from one transcript, a Droid session resumed in Claude Code: 883 `Execute` calls (536 shell, 191 validate, 75 write, 69 search, 12 read) and 159 `ApplyPatch` writes.
- **Episode splitting:** overlapping or touching stretches still merge, but a merged stretch is cut before a hit whose start is more than `EPISODE_SPLIT_GAP_CALLS` calls, or more than `EPISODE_SPLIT_GAP_SECONDS`, after the previous hit's start. Each piece takes the kinds of every hit it overlaps, so a long hit that spans a cut contributes its kind to both pieces. A single hit is never cut. The episode id stays the hash of the first call in the piece. Ids are stable while a transcript grows only until a new hit removes a cut: a hit whose start falls between two earlier hit starts (search_loop reports starts well before the latest call) can merge a later piece into an earlier one and orphan its id. This re-anchoring is accepted.

### Fire rates (final, v4)

Columns:

- **thr** is the share of threads with at least one hit (main n=1,536, sub n=5,844).
- **/100c** is hits per 100 live calls (main 114,993, sub 171,037).
- **ep/100s** is episodes per 100 sessions (n=1,536).

| kind | main thr | main /100c | sub thr | sub /100c | ep/100s main | ep/100s sub |
|---|---|---|---|---|---|---|
| tool_failure | 28.1% | 1.85 | 31.8% | 1.94 | | |
| retry_run | 0.3% | 0.01 | 0.3% | 0.06 | 0.5 | 6.6 |
| runaway_window | 0.7% | 0.01 | 0.4% | 0.01 | 1.2 | 1.4 |
| failure_streak | 1.4% | 0.02 | 0.6% | 0.03 | 1.6 | 2.5 |
| edit_churn | 3.2% | 0.07 | 1.8% | 0.07 | 5.1 | 7.4 |
| reread_churn | 0.1% | 0.00 | 0.1% | 0.00 | 0.1 | 0.5 |
| search_loop | 1.0% | 0.03 | 0.6% | 0.02 | 2.2 | 2.7 |
| unsupported_completion | 3.7% | 0.05 | 2.8% | 0.09 | 3.7 | 10.5 |
| mid_task_compaction | 0.4% | 0.02 | 0.2% | 0.01 | 1.1 | 0.8 |
| compaction | 6.3% | 0.21 | 0.3% | 0.01 | | |
| user_interruption | 9.2% | 0.28 | 0 | 0 | | |
| rejected_tool_use | 5.5% | 0.14 | 0.1% | 0.00 | | |
| permission_denial | 7.6% | 0.23 | 2.4% | 0.14 | | |
| rewind | 5.7% | 0.12 | 0 | 0 | | |

Against v3:

- search_loop fell from 4.7% of main and 6.0% of sub threads to 1.0% and 0.6%. It was 13.2% and 15.6% on v2.
- reread_churn fell from 1.0% and 1.1% to 0.1% and 0.1%. It was 4.3% and 5.5% on v2.
- Subagent rewinds fell from 45 hits (0.6% of sub threads) to none.
- tool_failure, failure_streak and runaway_window each dropped slightly, from the benign exits and the excluded cancellations.
- unsupported_completion, edit_churn and the point signals are unchanged.
- Episodes totalled 2,557 across 290 sessions on v2 and 1,114 across 202 on v3. They are now 669 across 160 sessions: 585 of at most 20 calls, 76 of 21–100, and 8 of 101–171. The largest is one 171-call search_loop hit; on v3 the largest merged 1,862 calls.

Other rates, unchanged from round 1:

- **Rewinds:** 87 of 1,536 sessions (5.7%) rewound on the main thread. A 300-session sample taken before the analysis was built measured about 7% (21/300). Of the 135 main-thread rewinds, 129 abandoned no tool calls; they are edited or re-sent prompts.
- **Rebuilds after idle:** 2,716 of 4,433 cache rebuilds (61%) followed more than 1 h idle. A 300-session sample taken before the analysis was built measured about 87% (987/1129).

### Split gaps

The gaps were read off a round-2 build without splitting:

- 71 episodes held more than one hit, with 94 gaps between consecutive hit starts: median 4 calls, p90 11, p99 36. Three were over 30 calls; the two inspected were a reread_churn hit starting deep inside a long search_loop.
- Of 61 distinct consecutive hit starts, all but six were at most 518 s apart. The six were 18 and 19 min (kept together), 42 and 43 min, 9.9 h and 20.9 h (split).

After splitting, the largest gap between consecutive hit starts inside an episode is 25 calls.

### Threshold sweep

The round-1 constants `REREAD_MIN` = 5 and `SEARCH_LOOP_MIN` = 20 compensated for the defects round 2 fixed, so lower values were re-measured on the v4 facts.

| Constant | Value | main threads | sub threads |
|---|---|---|---|
| `REREAD_MIN` | 3 / 4 / 5 | 18 / 3 / 1 | 44 / 13 / 4 |
| `SEARCH_LOOP_MIN` | 10 / 15 / 20 | 64 / 26 / 15 | 184 / 67 / 35 |

Of 8 sampled hits new at `REREAD_MIN` = 3, three were plausible rereads of one region. The rest were polling a background task's output file, re-reading a screenshot that a shell command re-took under the same name, and retries after a read failed for size. Both constants stay. The lower search-loop values were counted but not re-sampled for precision.

### Top-20 precision

The top 20 v4 episodes by severity, 10 on main and 10 on subagent threads, were checked against their call ranges in the index, with a transcript slice to confirm the line mapping.

- **Round 2: 19 TP, 1 FP.** Round 1 had 15 TP and 5 FP.
- **The true positives:**
  - repeated failed edits: not read, modified since read, string not found (7)
  - a shell that fails every command, even `echo` and `true`, retried (4)
  - the subagent concurrency limit, retried (3)
  - an edit, compile and fail loop through `ApplyPatch` and `Execute`, invisible before round 2
  - repeated reads of missing paths, process fork failures, an MCP auth loop
  - a subagent that kept crashing on spawn
- **The false positive** was a run of existence probes: `ls`, `cat`, `which` and `git config --get` on paths or keys that do not exist, which exit 1 by design.
- **Before the pipeline rule** the same read had two more probe-driven false positives, built largely from `grep … | head` with no match. With the rule, both fell out of the top 20.
- **Imprecise but true:**
  - A runaway window is not reset by a human message, so one episode ran past the user's next instruction.
  - An unsupported_completion hit anchored on a run of failed edits, because failed writes count as writes.
  - One episode joined real subagent crashes to a search_loop of an orchestrator reading files while its delegations failed.

### Constants changed

| Constant | Old → new | Round | Why |
|---|---|---|---|
| `signals.SEARCH_LOOP_MIN` | 10 → 20 | 1 | Most 10–19 runs were exploration or spanned a new user instruction. Kept in round 2. |
| `signals.REREAD_MIN` | 3 → 5 | 1 | Three reads were usually paging through one large file. Kept in round 2; see the sweep. |
| `health.PENALTIES["search_loop"]` | (4, 12) → (2, 6) | 1 | It remained the noisiest stretch signal even at 20. |
| `health.PENALTIES["reread_churn"]` | (2, 8) → (1, 4) | 1 | Paging remained a false-positive source. |
| `taxonomy.ERROR_CATEGORIES` | + 6 categories, 4 widened | 1 | Uncategorised errors fell from 19.7% to 1.2%. |
| `taxonomy.VALIDATION_PATTERN` | + package-manager check/type-check/lint/build/test scripts, `just <recipe>-*`; a keyword counts only when followed by `:` or `-` and a suffix, so `test:unit` matches and `npm run checkout` does not, vitest/jest/biome/pyright/playwright | 1 | Known validation was counted as "no validation". |
| `taxonomy.CODE_FILE_EXCLUDED_SUFFIXES` | + `.csv`, `.tsv`, `.log` | 1 | Data files are not code. |
| `signals.USER_CAUSED_CATEGORIES` | → `NON_AGENT_FAILURE_CATEGORIES`, + `sibling_cancelled` | 2 | A cancelled sibling is a cascade of another call's error. |
| `signals.DEFAULT_READ_LIMIT` | new, 2000 | 2 | The line range of a read without a `limit`. |
| `episodes.EPISODE_SPLIT_GAP_CALLS` | new, 30 | 2 | Cuts only the 3 of 94 hit-start gaps above 30 (p90 11, max 36). |
| `episodes.EPISODE_SPLIT_GAP_SECONDS` | new, 1800 | 2 | Hit-start gaps were at most 19 min except four idle breaks of 42 min to 21 h. |
| `taxonomy.SHELL_TOOLS` / `WRITE_TOOLS` | + `Execute` / + `ApplyPatch` (`PATCH_TOOLS`) | 2 | Their calls fell to kind `other`. |
| `taxonomy.TRAILING_SEARCH_COMMANDS`, `PIPE_FILTERS`, `GIT_VALUE_FLAGS`, `NULL_SINKS`, `WRITE_REDIRECTS`, `HEREDOC_START`, `PATCH_FILE_LINE` | new | 2 | Benign exits, shell writes and patch paths. |

`FACTS_VERSION` 2 → 3 → 4 → 5, because the taxonomy shapes stored facts and round 2 added the `read_offset` and `read_limit` columns. `DERIVE_VERSION` 2 → 3 → 4 → 5; round 3 changed the pipe-fed benign exit, the validation suffix rule, `Execute` exit codes, shell redirects, quoted heredocs and PDF rereads. Every version is pinned in `tests/analysis/test_versions.py`.

### Deferred precision notes

- **Existence probes:** `test -f`, `ls missing` and similar probes that fail by design still count as tool failures.
- **Failed writes:** a write call that errored still counts as a write for search_loop's first write, edit_churn and unsupported_completion.
- **Runaway windows:** a human message does not reset a runaway_window.
- **Droid `Create`:** Droid's `Create` tool is kind `other`, so it is invisible to write-based signals (edit_churn, unsupported_completion, search_loop's first write).
- **Archived prefix:** `rl analyze session <prefix>` resolved an archived and a live session in round 1; round 3 did not change resolution and did not re-run it, and no fixture covers it.

### Codex calibration

The Codex extractor repeated the protocol with `--source codex` on 2026-10-18, on `8af3954` plus the Codex extractor (committed as `db91fab`). Codex rollouts are now indexed (`coverage.codex` is `full`), and no `rl analyze` run reads them through the tokens-only path any more.

#### What the rollouts hold

A read-only survey of the rollouts, using types and keys only, found the following:

- **Fork replay:** `session_meta.payload.subagent_history_start_ordinal` is the line index of the first own event (the own `thread_settings_applied`). The lines before it are the parent's history: a second `session_meta`, `compacted`, `turn_context`, messages, items and `task_complete`. The extractor skips them through `usage.codex.own_events_start`, which the usage reader also uses.
- **Thread ids:** `item_completed`, `thread_settings_applied` and `token_usage_record` carry `thread_id`, while `token_count` does not.
- **Identity:** the meta's `session_id` is the root thread id for every spawned rollout, including depth-2 spawns. A rollout's own `id` always matches the filename UUID. Guardian reviewers (`source.subagent.other`) name their parent only in the top-level `parent_thread_id`.
- **Kinds of rollout:** 4,238 rollouts, of which 35 are archived. 387 are interactive mains (`cli`, `vscode`), 1,129 are `codex exec` runs and 2,722 are subagents: 2,472 depth-1 spawns, 18 depth-2 spawns and 232 guardians.
- **Tool calls:** code mode wraps most tool use in a `custom_tool_call` named `exec` that runs a script. Its `CommandExecution`, `FileChange` and `McpToolCall` items carry their own `exec-…` ids, so they never match the wrapper's `call_id`. A direct `exec_command` function call does match its item by id. Older rollouts have only response items (`shell`, `shell_command`, `apply_patch`, `update_plan`, `write_stdin`) and no items.
- **Compactions:** every `ContextCompaction` item comes right after a `compacted` record. Counting both would double every compaction.

#### Extractor rules

- **Code-mode wrappers** (`exec`, `wait`) are not calls: their work is in the items. A wrapper counts as an error call only when its output starts `Script failed` and no tool item completed while it ran (456 calls).
- **Response-only calls:** shell calls take their exit code from the output's `metadata.exit_code`, or from a leading `Exit code:` or `Process exited with code` line. With no exit code, the call is an error only when the output starts with a known failure prefix (`CODEX_FAILED_OUTPUT`). A legacy `apply_patch` call writes one call per `*** … File:` line. Plain-text output loses its harness header (`Exit code:`, `Chunk ID:`, `Wall time:`, `Output:`) before the benign-exit rules and the error signature see it.
- **Agent tools:** `spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `close_agent`, `interrupt_agent` and `list_agents` are kind `agent`. A `SubAgentActivity` item of kind `completed` is a notification, not a call.
- **Other items:** `WebSearch` and the `web.search` extension are `web_search` (search), `ImageView` is `view_image` (read), and the `clock.sleep` extension is `sleep`.
- **Kinds from `parsed_cmd`:** they refine a command only when every entry is `read`, `search` or `list_files`, and only when the command alone would be shell, read or search. Validation and shell writes keep their own kind.
- **Retry identity:** a shell call's `input_hash` covers its command and its working directory. A script that runs the same command in eight directories is not a retry.
- **Subagents:** a rollout with a `source.subagent` and a parent id is a subagent, guardians included. `origin` is `agent` for all its messages.
- **Compactions:** a `compacted` record counts, and the `ContextCompaction` item beside it, before or after and across any `token_count` in between, is the same compaction.
- **Declined items:** an item with status `declined` is a `user_rejected` error, not an agent failure.
- **Human messages:** rollouts without `UserMessage` items take user prompts from `response_item` user messages; rollouts with items ignore those, so nothing counts twice.
- **Error text:** the tail of the output, cut at a line boundary, after an `Exit code N` line, so that it reads like a Claude Bash error.
- **Stored items:** large outputs are clipped to head and tail before they are kept, so a 180 MB rollout does not hold its whole output in memory.

#### Timings and size

| | Codex slice |
|---|---|
| Cold build, all agents, wall (`rl analyze --all --json`) | 213.9 s (ingest 171.4 s) |
| Transcripts indexed, `index.failed` | 11,641, 0 |
| Index file | 859 MB (536 MB Claude-only) |
| No-change `--all`, wall | 41.2–47.9 s (ingest 1.4–4.3 s) |
| No-change `--all --source codex`, wall | 16.7 s (ingest 0.9 s) |
| Default window (7 days), all sources / codex | 12.7 s / 3.3 s |
| `rl usage --all --source codex`, wall | 81.5 s |

The cold build stays under the 5-minute bar. The no-change `--all` run fell from 64–70 s to 41–48 s, because the Codex tokens-only read is gone from `rl analyze`. `rl usage --source codex` still pays for `own_events_start`.

A second cold build after the tuning took 896 s of wall time but only 215 s of CPU, on a machine busy with other work. No-change runs at that time took 61–188 s of wall time against 19–38 s of CPU. The table uses the first, uncontended pass, and the tuning does not change what a run costs.

#### Parity

| Window | `rl analyze` `totals.tokens.total` | `rl usage` `total_tokens` |
|---|---|---|
| Codex, 2026-01-01..2026-10-17 | 12,711,542,900 | 12,711,542,900 |
| Codex, `--all` | 13,418,709,499 | 13,418,709,499 |

Both windows match exactly, before and after the tuning. They also equal the earlier tokens-only figures, so forked rollouts are counted once in both readers.

#### Errors

The store holds 153,897 Codex calls, of which 9,205 are errors and 5 are unresolved. Before tuning, 531 errors (5.8%) had no category. Most were `apply_patch verification failed: Failed to find expected lines`, `invalid patch:` and `collab spawn failed`. Widening `string_not_found`, `input_validation` and `subagent_error` cut that to 147 (1.6%).

`permission_denied` is common in Codex, at 552 errors. Most of these are real sandbox denials: `zsh: operation not permitted: ps`, `failed in sandbox`, `PermissionError: [Errno 1] Operation not permitted`, `EPERM`, `sandbox_apply`. 90 of them also print a Homebrew `shellenv` line about `/bin/ps`, which the agent did not cause. These errors count as not-agent failures, as they do for Claude.

#### Fire rates (final, v6)

Columns:

- **thr** is the share of threads with at least one hit (main n=387, exec n=1,129, sub n=2,722).
- **/100c** is hits per 100 live calls (main 84,842, exec 3,232, sub 65,823).

| kind | main thr | main /100c | exec thr | exec /100c | sub thr | sub /100c |
|---|---|---|---|---|---|---|
| tool_failure | 67.7% | 5.43 | 6.6% | 4.58 | 42.9% | 5.88 |
| retry_run | 2.8% | 0.02 | 0 | 0 | 0.3% | 0.01 |
| runaway_window | 9.0% | 0.08 | 0.3% | 0.09 | 2.2% | 0.11 |
| failure_streak | 12.4% | 0.12 | 0.4% | 0.15 | 3.2% | 0.16 |
| edit_churn | 12.7% | 0.15 | 0 | 0 | 2.9% | 0.17 |
| reread_churn | 0.3% | 0.00 | 0 | 0 | 0 | 0 |
| search_loop | 11.6% | 0.14 | 0 | 0 | 0.6% | 0.03 |
| unsupported_completion | 2.8% | 0.01 | 0 | 0 | 1.9% | 0.08 |
| mid_task_compaction | 4.7% | 0.04 | 0 | 0 | 0 | 0 |
| compaction | 32.0% | 0.68 | 0 | 0 | 2.9% | 0.15 |
| user_interruption | 16.8% | 0.16 | 0.4% | 0.15 | 0 | 0 |
| permission_denial | 26.4% | 0.49 | 1.3% | 0.99 | 3.0% | 0.15 |

Episodes: 399 on interactive main threads, 6 on exec runs and 290 on subagents. Before tuning these were 840, 8 and 308.

Against Claude:

- Codex main threads are long, about 219 calls each against about 75 for Claude. That alone raises the thread shares.
- Per 100 calls, `tool_failure` is about three times Claude's. Codex runs reads, searches and tests through the shell, and every non-zero exit there is an error, where Claude's Read and Grep tools rarely fail.
- `compaction` is common because long interactive sessions auto-compact.
- `exec` runs are short scripted prompts. They almost never fire a stretch signal.

#### Top-10 precision

The top 10 Codex episodes by severity were checked against their call ranges in the index, with transcript slices to confirm the line mapping.

- **Before tuning: 9 TP, 1 FP.** The false positive was a code-mode script that ran one `git status; git diff; sed` command in eight directories, which read as an eight-fold retry. The next script ran a drift check that exits 1 in the same directories, which read as a failure streak.
- **After tuning: 10 TP, 2 of them imprecise.** The 9 earlier true positives were unchanged, and a new #10 joined: an agent working in a directory that was not a git repository, failing `git` calls repeatedly during a 49-read search loop.
- **The true positives:**
  - `gh api` calls with an unquoted `?` that zsh rejected, repeated across many repositories (2)
  - one Python module crashing on every run
  - a conflicted `git rebase` and `git apply` sequence
  - an invalid `gh search` query retried four times
  - an emulator that stayed offline under `adb`
  - a package install and QA script failing between edits
  - a local server that was not running, probed with `curl`
- **Imprecise:** two episodes mix real environment failures with existence probes (`which`, `test -f`) and an interrupted install (exit 130). These are the deferred existence-probe notes.
- **search_loop sample:** four random main-thread hits (median 26 reads, max 65) were reviews and documentation reading after an early write. They are plausible, but weak as problems. Codex has no constant of its own for this, and the penalty is capped at 6, so it was left alone.

#### Constants changed

| Constant | Change | Why |
|---|---|---|
| `signals.CODEX_POLLING_TOOLS` | new: `wait_agent`, `wait`, `sleep`, `write_stdin`, `list_agents`; these never form a `retry_run` | 435 of 538 Codex retry runs were `wait_agent` polling. The main-thread rate fell from 18.3% to 2.8%. |
| `taxonomy.ERROR_CATEGORIES` | `string_not_found` + `Failed to find expected lines`; `input_validation` + `invalid patch:`; `subagent_error` + `collab spawn failed`; `interruption` + a leading `aborted by user` | Uncategorised Codex errors fell from 5.8% to 1.6%. |
| `taxonomy` Codex tables | new: `CODEX_SHELL_CALLS`, `CODEX_SCRIPT_CALLS`, `CODEX_SHELLS`, `CODEX_SHELL_FLAGS`, `PARSED_CMD_KINDS`, `CODEX_OK_STATUSES`, `CODEX_OUTPUT_EXIT`, `CODEX_FAILED_OUTPUT`, `CODEX_INJECTED_PREFIXES`; Codex tool names in `READ_TOOLS`, `SEARCH_TOOLS`, `PATCH_TOOLS`, `SHELL_TOOLS` and `AGENT_TOOLS` | Codex tool names and `parsed_cmd`. |

`FACTS_VERSION` 5 → 6 → 7 (taxonomy and Codex facts; 7 added `declined` items, harness-header stripping and the response-message fallback) → 8 (the 1h cache-write clamp shared with `rl usage`, and Codex response outputs clipped on arrival). `DERIVE_VERSION` 5 → 6 (`CODEX_POLLING_TOOLS`). Both are pinned in `tests/analysis/test_versions.py`.

#### Codex precision notes

- **Fan-out failures:** a script that runs one check in many directories, where the check exits non-zero by design, still reads as a failure streak.
- **search_loop on long main threads:** see the sample above.
- **Scripted prompts:** `codex exec` prompts count as human messages, as `claude -p` prompts do.

### Codex isolation

The protocol matched the Claude rounds. `XDG_DATA_HOME` and `--index` pointed into a temp dir that held a copy of the registry and symlinks to the archive and the opencode data dir. Only `rl analyze` and `rl usage` ran. Everything directly under the live state dir, the archive dir and an md5 of the archive listing were `stat`ed before and after, and the diff was empty. No `analysis/` dir appeared under it. The temp dir was removed afterwards, with its symlinks unlinked first.

### Rollup and replay-cache performance

`rl analyze` scans the deduped requests once for the totals, cache stats and every dimension; `worst_sessions` does its own scan for per-session tokens. `rollups.corpus_rollups` reads the deduped requests and calls a single time, selects only the columns the rollups use, and feeds the totals, the cache stats and every dimension from that one scan. Outputs did not change. The separate `rollup`, `totals`, `cache_stats` and `thread_windows` entry points were removed later, once `corpus_rollups` was their only production caller.

`StartCache` in `usage/codex.py` stores each forked rollout's `own_events_start` result in `codex-own-starts.json` under the analysis state dir, keyed by path and checked against the file's stat fingerprint (device, inode, size, `mtime_ns`, `ctime_ns`). `rl usage` writes it and the Codex extractor only reads it. The file carries `REPLAY_VERSION`, pinned by a hash of the replay logic, and is dropped on a mismatch; entries for deleted rollouts are dropped on write. A rollout whose first record is not a fork is never read past that line and never cached. The file is written atomically, a corrupt file is ignored and rebuilt, and an unwritable state dir only skips caching. Nothing is written under `~/.codex`.

| | Before | After |
|---|---|---|
| No-change `--all`, wall | 41.2–47.9 s (42 s on the re-run baseline) | 15 s |
| Default window (7 days), all sources | 12.7 s (14 s on the re-run baseline) | 4 s |
| `rl usage --all --source codex`, first run (fills the cache) | 81.5 s (38 s on the re-run baseline) | 21 s |
| `rl usage --all --source codex`, cached | | 16 s |

The baseline was re-run on the same machine just before the change, with a warm page cache, which is why it sits below the 81.5 s figure. The cold build took 205 s. Totals, cache stats, every dimension and the worst sessions are identical before and after, for the default dimensions and for all eight, and `rl usage` output is byte-identical.

### Resolved follow-ups

- **Shared rollup scan:** one deduped request and call scan feeds the totals and all dimensions.
- **Codex replay detection:** `own_events_start` results are cached per rollout fingerprint.
