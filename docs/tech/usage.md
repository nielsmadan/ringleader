# Usage analytics

`ringleader/usage/` reads local token accounting and quota captures, plus on-demand
subscription and OpenRouter reports.

## Purpose

Answer "where did my tokens go" without an API call, by parsing what each agent already wrote to
disk. Five readers normalize into one `UsageRecord` stream that the aggregation and rendering
layers treat identically.

Remote reports use provider-specific units and scopes. The CLI owns the remote fetch and cache.

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
| `codex.py` | `~/.codex/sessions/**/*.jsonl` and `.jsonl.zst` (plain wins when both exist); record ids keep the plain path, so compression changes no id |
| `droid.py` | `~/.factory/sessions/*/<uuid>.settings.json` sidecars, not the transcripts |
| `opencode.py` | `$XDG_DATA_HOME/opencode/opencode.db` in SQLite read-only mode |
| `pi.py` | `~/.pi/agent/sessions/**` |

### Local checkpoints

[`checkpoint.py`](../../ringleader/usage/checkpoint.py) keeps parsed usage in
`usage/local-v2.sqlite` under the state directory. Each namespace includes the reader and both
the absolute path and original spelling of the configured source root. This keeps live and
archived sources independent and preserves path-based export identities for relative roots.
Within a namespace, a checkpoint matches an absolute input path, a variant and the input
fingerprint. Fingerprints use device, inode, size, nanosecond mtime and ctime, and include
companion files where applicable. A missing companion has its own signature value.

- Claude keeps the first occurrence of each request identity within a file and deduplicates
  across files again in sorted path order for each report.
- Codex usage variants include `REPLAY_VERSION`. Latest captured windows use a separate
  namespace and store only the latest snapshot per window kind within each rollout.
  An I/O failure reading project metadata still yields uncached usage with an unknown
  project. Transient failures in fork detection or window extraction also leave the
  rollout eligible for retry.
- Droid fingerprints both the transcript and settings sidecar. It stores the header session ID
  and the optional normalized record. If sidecar data is unreadable, malformed or has a
  counter that fails integer conversion, the first sorted path still owns the session when
  its header is readable. A later scan retries that read. Each yielded record is a copy of
  the reader's memoized record.
- OpenCode fingerprints the resolved database and its actual `-wal`, including when the
  configured database path is a symlink. Reads keep their SQL date and session filters.
  Summary and session-prefix queries each retain up to four recently written filter variants
  per database, so one-off session lookups cannot evict recurring summaries. The latest
  response has a separate namespace.

Readers rediscover source files on each invocation. They read selected changed files from the
start and stream records as they parse. Results over 1 MiB of encoded payload still stream and
are read again next time. Completed entries flush in short transactions at 64 entries or 1 MiB
of pending payload. Context exit flushes completed entries and closes SQLite, including on
early iterator exit. A completed read must pass a fingerprint check at the end of parsing and
again under the SQLite write lock. The next scan retries any incomplete input. Cleanup takes
the same lock before confirming that source paths are missing and pruning their entries in
the active namespace. Vacuuming runs when SQLite has free pages to reclaim. Warm hits with
no pending cleanup use read queries alone.

The configured and resolved state paths must have ancestors owned by the current user or root.
Group or world writable ancestors require the sticky bit. The cache verifies ownership and
applies directory mode `0700` and database mode `0600` through open descriptors. Existing cache
files must be singly linked regular files with a SQLite header and the cache application ID.
New databases get their identity, schema and incremental autovacuum setting in a private
temporary file before an atomic link publishes them at the cache path.

SQLite waits up to 100 ms for a busy lock. An unavailable cache falls back to source reads.
Checksums and typed decoders make an invalid entry a cache miss and a successful reread can
replace it.

Bump `checkpoint.VERSION` when changing persisted shapes or extraction semantics. The
[version pin](../../tests/test_usage_checkpoint.py) covers the schema, stored record fields and
typed decoders. Each version uses its own file. Removing `usage/local-v2.sqlite` while usage
commands are idle forces a cold read. The cache stores derived data and leaves agent inputs
intact. See the [repeatable checkpoint benchmark](../tests/local-usage-checkpoints/README.md).

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

`estimate()` prices `cache_write_1h` (a subset of `cache_write`, read from
`usage.cache_creation.ephemeral_1h_input_tokens`) at 2x the input price and the remaining cache
writes at the table's cache-write price (1.25x). `model_family()` also accepts the bare aliases
`opus`, `sonnet`, `haiku`, `fable` and `mythos`. Exports written before `cache_write_1h` existed
load with it at 0.

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
- `commands/usage.py` — local accounting and remote command routing.

### Remote reporting foundations

Bare `rl usage`, `rl usage local` and `rl data tokens` share `local_summary()`.
The remote models in `remote_types.py` are separate from `UsageRecord` and
`WindowSnapshot`. Their version 1 JSON explicitly serializes validated data, preserves
unknown values as null, rejects boolean/nonfinite numbers and uses opaque identity labels.
The standalone document has `version: 1`, `kind: subscription|token` and a `results` array.
Every result carries provider, scope, identity, status, fetched_at, cached, stale, data and
diagnostic fields. Status is `ok`, `not_configured`, `uncached` or `error`; diagnostics carry
code, message and nullable retry_at. Data follows the scope's dataclass in `remote_types.py`.
The JSON goldens in `tests/goldens/usage/` pin subscription window fields and the three
OpenRouter report shapes, including null versus zero. Legacy local JSON retains its existing
shape.

`remote_http.py` launches an internal fixed-endpoint GET worker with a 15-second parent
deadline. The parent and worker both check the production endpoint allowlist. Fixture tests
inject a transport or connection. Request headers travel through framed private
stdin and bounded stdout carries the reply. The HTTP and Keychain helpers receive an allowlisted
environment. The worker verifies TLS, refuses redirects and reads bodies for successful responses.
Throttling replies return their status and Retry-After headers without a body read. Ringleader
maps failures to fixed diagnostics and discards subprocess stderr. Timeout and
cancellation kill and reap the child.

`remote_cache.py` stores normalized snapshots in `usage/remote-v1.json` under the state dir.
Snapshots are partitioned by provider, report, credential/profile fingerprint and query bounds.
`Retry-After` on HTTP 429 or 503 sets endpoint backoff for up to 24 hours, across query ranges
and explicit refreshes. The retry time tells the user when to run the command again. Successful
snapshots survive failed refreshes, whose attached data remains explicitly stale. Cache-only
commands read the cache once and select the last fetched scope without consulting credentials.
The compact internal format remains version 1 and reads earlier version 1 snapshots. Older
development builds require the earlier encoding. A dedicated nonblocking lock waits at most
one second and protects reload/merge plus owner-only atomic replacement.
The cache validates stored timestamps against the wall clock after reading them. Writes
keep the original attempt time for ordering, so overlapping attempts can save independent
results and late older successes cannot replace newer ones. A successful replacement of an
invalid cache reports `cache_repaired`. An unsupported cache version is preserved and reported safely.
An ordinary endpoint failure preserves overlapping Retry-After instructions. A newer success
clears earlier instructions for that endpoint.

The config stores only `usage.claude_auth`, `usage.codex_auth` and `usage.cache_seconds`
preferences. Validation is lazy at the remote command boundary, so malformed remote settings
cannot break local accounting; `rl config set` validates values before saving them.

### Subscription providers

`subscription.py` coordinates independent Claude and Codex quota reports. Claude can
reuse a fresh snapshot for the resolved OAuth credential and profile. Each online Codex
call fetches because the account ID is only available from the app-server response. If a
successful reply has a missing or empty `accountId`, the snapshot uses the `unknown`
cache key and a null report identity. Cached output shows the last fetched identity and
marks elapsed reset times as expired without showing them as current utilization.

`claude_credentials.py` reads the environment token or the selected agent profile.
Agent credentials require a future expiry and the `user:profile` OAuth scope. On macOS the
native Keychain path requires both Claude profile overrides to be unset and `HOME` to match the
real UID's account home. `claude_keychain.py` uses an exact service query in a bounded,
noninteractive helper and reports access denials or multiple matches. Custom profiles use
the exact credentials file. Tests isolate profile state and stub the native credential reader.
The OAuth adapter makes one GET to the fixed usage endpoint and normalizes overall and
model-scoped quota windows. Flat model windows fill missing scoped fields by exact normalized
model ID first, then by a display alias with a single owner. Ambiguous flat windows stay separate.
The credential path, request headers and quota parser adapt CodexBar at the revision
and source paths recorded in [Third-party notices](../../THIRD_PARTY_NOTICES.md).

`codex_subscription.py` verifies Codex CLI 0.170.0 or newer, initializes its bounded
stdio app-server client with explicit gateway OAuth, and requests only account quota.
It prefers current metered buckets over the legacy single-bucket reply. The subprocess
uses normal Codex configuration with Claude and OpenRouter report credentials removed from its
environment. Startup may load plugins and Codex may persist its ordinary auth refresh.
Ringleader sends no login or inference requests.

### OpenRouter reports

`token.py` coordinates independent key, activity, and credits reports. `openrouter.py`
uses the fixed `/api/v1/key` endpoint with the ordinary API key and the fixed
`/api/v1/activity` and `/api/v1/credits` endpoints with the management key. Results use
a credential fingerprint for identity and keep the scopes and OpenRouter and external BYOK
figures separate. [Key API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)
[Activity API](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity-grouped-by-endpoint)
[Credits API](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)

Activity uses one request for the provider's rolling 30 completed UTC days, validates
calendar dates, and filters rows to the inclusive requested range. If the UTC date
changes during the request, the report returns a diagnostic and a subsequent invocation
can request the new period. Cache lookup uses exact dates online and the requested day
count offline. Endpoint backoff applies across date ranges for the same credential.
Allowed fields are validated and strings containing supplied credentials are rejected
before any output or cache write.

An actual report error returns exit 1 with other results intact. Cache-only read errors
also return exit 1. A successful live report with a cache warning retains status `ok`
and is usable. Cold cache-only lookups carry status `uncached` and are informational.

### Inbox usage views

`home/screens/usage.py` uses the same cancellable CLI runner as the command palette.
Local runs the two offline reports in sequence, restores completed output on return to the
view, and reruns them on `r`. Subscription and Token request cache-only results on activation.
`r` starts a remote refresh, `c` cancels, and switching views invalidates the previous job's
generation. The private `--_inbox` flag returns a bounded, structured subprocess reply and is
mutually exclusive with public `--json`. The screen retains the last displayed data by provider
and scope. Retained snapshots keep their original identity and fetch time under **Previous report**
while loading or after cancellation. They also appear when completion does not supply a snapshot
at least as recent as the retained one. The inbox displays at most 30 activity rows and reports the
full row count. It offers the cache-only command for the latest saved report or an explicit fetch
command when saving the preview failed. The public CLI renders the full activity report in text
and JSON.
The CLI manages the cache and the runner manages processes. Usage reads run outside the registry
writer queue.

Strict Textual tests cover navigation, cancellation, late completion, narrow rendering,
and CLI output through an injected fixture transport. The real PTY scenario exercises the
same CLI with isolated stores and empty credentials. For provider documentation and opt-in
manual checks, see [provider usage](../reference/provider-usage.md).

## Gotchas

- **Captured Claude windows need `rl install statusline` first**, and only populate after the first API
  response in a session. Before that, `rl usage windows` reports nothing for Claude and says so.
  Pro/Max only, Claude Code v2.1.80+.
- **A percentage over 1000 is an epoch**, not a percentage — see `_EPOCH_LEAK_FLOOR`.
- **`PRICES` is a static table.** Cost figures are estimates and drift with provider pricing.
- **`--days` defaults to 7**, so a bare `rl usage` is a week, not all time. `--all` widens it.
- **Local usage discovers files on every run and reuses eligible checkpoints.** The default
  seven-day window is anchored on `latest_day()`; file readers retain their mtime prefilter.
  `--archived` and `--merge` anchor on pooled records and deduplicate the combined set. Droid
  considers every header/sidecar checkpoint because its first-path session ownership must stay
  stable. OpenCode retains its SQL response-date filter when refreshing a query checkpoint.
  A cold `--all` run still reads the whole selected history.
- **Claude subagents count toward the parent.** `*/<sid>/subagents/agent-*.jsonl` and
  `*/<sid>/subagents/workflows/*/agent-*.jsonl` are read with the main transcripts, and
  `claude.session_of()` credits them to `<sid>`. Files are ordered by `str(path)` before the
  shared `(message id, requestId)` dedupe, so `<sid>.jsonl` is read before `<sid>/subagents/...`.
- **Codex forks count once.** A forked or spawned rollout replays its parent's history before its
  own events. `codex.own_events_start()` finds where they begin (the `subagent_history_start_ordinal`
  in `session_meta`, else the first `thread_settings_applied` carrying the rollout's own id; 0
  when the rollout is not a fork) and earlier lines are skipped. An ordinal at or past the last
  line is not trusted: the start is then the first event stamped more than a second after
  `session_meta`. In observed rollouts `token_count`
  events carry no thread id, so `own_thread()` only filters payloads that do. Each token_count takes
  the model of the latest `turn_context` after the start. `windows()` is unaffected.
  Each fork's start is cached in `analysis/codex-own-starts.json` under the state dir
  (`StartCache`, keyed by path and stat fingerprint, versioned by `REPLAY_VERSION`), so a later run
  re-reads a fork's head only when the file changed. A reader loads the cache on first use and
  keeps it for `latest_day` and `records`; `rl usage` writes it once, at the end of the command,
  with a private atomic write. `rl analyze` reads the same file and never writes it; see
  [analysis](analysis.md#codex-start-cache).
- **Pi nested runs** (`<proj>/<stem>/<hash>/run-N/session.jsonl`) take the parent session's id. Reviewer transcripts
  (`<proj>/subagent-artifacts/<uuid>_reviewer_transcript.jsonl`) are not read: each duplicates a
  nested run, so counting both double-counts the reviewer's tokens.
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
- [reference/provider-usage.md](../reference/provider-usage.md)
- [reference/agent-session-management.md](../reference/agent-session-management.md) — where each
  agent puts its transcripts
