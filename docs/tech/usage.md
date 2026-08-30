# Usage analytics

`ringleader/usage/`: reading token spend out of four agents' local transcripts, plus
subscription-window state.

## Purpose

Answer "where did my tokens go" without an API call, by parsing what each agent already wrote to
disk. Four readers normalize into one `UsageRecord` stream that the aggregation and rendering
layers treat identically.

## How it works

### The record

`UsageRecord` (`base.py`) is the normalized unit: source, session id, project, model, timestamp,
and the four token counts — `input`, `cache_read`, `cache_write`, `output`. They are kept
separate rather than summed because cache reads and writes price very differently;
`total_of()` combines them when a single number is wanted.

`UsageReader` is the protocol each harness implements. `aggregate.READERS` maps a source name to
its reader, and is the authority for `--source`'s valid values.

### Readers

| Module | Reads |
|---|---|
| `claude.py` | `~/.claude/projects/*.jsonl` usage records |
| `codex.py` | `~/.codex/sessions/**/*.jsonl` |
| `opencode.py` | the OpenCode store |
| `pi.py` | `~/.pi/agent/sessions/**` |

### Aggregation

`rollup(records, key)` buckets a record stream by any key function into `Bucket` totals;
`day_of()` and the `_KEYS` map in `commands/usage.py` supply the `--by day|model|project|
session|source` options. `in_window()` applies `--since` / `--until`.

`pricing.py` maps a model string to a family (`model_family()`) and estimates cost
(`estimate()`). Prices are a hardcoded table — deliberately, so the tool needs no network — and
therefore go stale when a provider reprices. An unrecognized model yields `None` rather than a
wrong number.

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
- **Reading is per-transcript**, so usage over a large history is I/O-bound in the same way a
  scan is.

## Why

Parsing local transcripts rather than querying a billing API: it needs no credential, works
offline, covers agents that expose no API at all, and keeps working when a provider changes its
reporting surface. The cost is that pricing is a table we maintain.

## Related

- [features/usage-analytics.md](../features/usage-analytics.md)
- [reference/agent-session-management.md](../reference/agent-session-management.md) — where each
  agent puts its transcripts
