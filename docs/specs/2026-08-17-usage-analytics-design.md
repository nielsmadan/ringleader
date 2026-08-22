# Usage Analytics — Design

**Goal:** A multi-harness, on-disk token-usage view for ringleader — `rl usage` —
that reports where tokens go across Claude Code, Codex, OpenCode, and Pi, with an
input/output/cache/reasoning breakdown, a heavy-session drill-down, Codex
subscription-window status, and a `ringleader-usage` skill that interprets it.

**Scope:** Sub-project 1 of the broader "usage" feature — the half that is fully
buildable from local transcripts today. Deferred to later sub-projects (out of scope
here): capturing Claude's live 5-hour/weekly window state (needs a proxy/hook, never
persisted to disk), and pulling provider usage APIs (needs admin/org keys, against
ringleader's no-secrets stance).

## Data reality (grounding)

All four harnesses persist per-message token usage on disk, mapping onto a common
schema:

- **Claude Code** (`~/.claude/projects/*/*.jsonl`, assistant records): `message.usage`
  = `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`; plus `message.model`, `message.id`, `requestId`,
  `timestamp`. No discrete reasoning field (thinking is folded into `output_tokens`).
  Tools appear only as request *counts* (`server_tool_use`), never token totals.
- **Codex** (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, `event_msg` records of
  `type:"token_count"`): `payload.info.last_token_usage` / `total_token_usage` with
  `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`, `total_tokens`; plus `model_context_window`. The same
  record also carries `rate_limits { primary/secondary { used_percent, window_minutes,
  resets_at }, plan_type, credits }` — the only harness that persists subscription-window
  state.
- **OpenCode** (`~/.local/share/opencode/storage/message/<ses>/<msg>.json`):
  `tokens { total, input, output, reasoning, cache { read, write } }`, `cost`,
  `modelID`, `providerID`, `time`.
- **Pi** (`~/.pi/agent/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl`, assistant messages):
  `usage { input, output, cacheRead, cacheWrite, reasoning, totalTokens, cost{...} }`,
  `provider`, `model`, `api`.

**Consequences that shape the design:** per-tool token attribution does not exist on
disk for any harness and is out of scope. Claude has no reasoning field (record 0).
Only Codex exposes subscription windows on disk. Cost is stored on disk for OpenCode and
Pi, computed from a price table for Claude and Codex, and omitted where unknown.

## Architecture

A dedicated `usage/` package — decoupled from the session-triage `Source` seam, since
usage is a different concern and OpenCode/Pi have no `Source` implementation. `tokens.py`
(today's Claude-only ccusage-style accounting) is superseded: its pricing, `Bucket`, and
rollup logic move into `usage/`, and `rl data tokens` becomes a thin alias.

```
usage/
  base.py        # UsageRecord, WindowSnapshot, UsageReader protocol
  claude.py      # one reader per harness — each globs its files and yields
  codex.py       #   already-deduped UsageRecords (+ WindowSnapshots for codex)
  opencode.py
  pi.py
  pricing.py     # price tables (Claude, Codex); OpenCode/Pi use on-disk cost
  aggregate.py   # rollup/bucketing (absorbs tokens.py's Bucket/rollup)
commands/usage.py
.agents/skills/ringleader-usage/
```

Each reader owns: (1) default file location (patchable for tests), (2) discovery, and
(3) parse+normalize+dedup. A future harness is one new file plus one registry entry.

## Data model

```python
@dataclass
class UsageRecord:
    source: str            # claude | codex | opencode | pi
    session_id: str
    project: str | None    # decoded cwd/label
    timestamp: str         # ISO, per assistant turn
    provider: str | None
    model: str | None
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int         # 0 for Claude (folded into output on disk)
    total: int
    cost: float | None     # on-disk if present, else computed, else None
```

Readers yield **already-deduped** records. Claude dedups by `(message id, requestId)`
(Claude Code rewrites records across resumes/summaries; a raw sum double-counts). Codex
sums per-turn `last_token_usage` deltas, never the cumulative `total_token_usage`.

```python
@dataclass
class WindowSnapshot:            # Codex only
    source: str                 # "codex"
    timestamp: str
    plan_type: str | None
    window: str                 # "5h" | "weekly"
    window_minutes: int
    used_percent: float
    resets_at: str              # ISO (from epoch)
```

## Command surface

`rl usage` is a top-level command (a new pillar alongside `overview`/`triage`).

- **`rl usage [--source S] [--days N | --since D | --until D | --all] [--by day|project|session|model|source] [--breakdown] [--limit N] [--json]`**
  — totals + by-day + a ranked table. `--breakdown` adds input/output/cache/reasoning
  columns. Default: all sources, trailing 7 days, `--by project`. Tokens are the primary
  unit; a cost column appears only where cost is known.
- **`rl usage session <id> [--json]`** — the heavy-session breakdown: total + split,
  cache hit-rate (`cache_read / (cache_read + input + cache_write)`), top turns by
  output and by total, context growth across the session (input+cache_read per turn as a
  proxy), and — Claude, best-effort — re-read / large-tool-output hotspots derived from
  `tool_use`/`tool_result` records (same path read repeatedly, large uncached results).
- **`rl usage windows [--json]`** — Codex 5h + weekly status (used %, resets-in, plan
  tier). States plainly that Claude's windows are not on disk.
- **Cross-machine (minimal, file-based — not a sync service):** `rl usage export
  [--out PATH]` emits normalized records plus a machine id as JSON; `rl usage
  --merge <dir>` folds other machines' exports into any view, deduped by
  `(machine, source, session_id, timestamp)`.

`rl data tokens` stays as an alias mapping to `rl usage --source claude`, preserving the
current behaviour and flags.

## `ringleader-usage` skill

An agent-neutral skill bundled like the other `ringleader-*` skills (a `SKILL.md` plus a
`references/` playbook, wired in via `rl skills install`). Flow: run `rl usage` to spot
heavy sessions → `rl usage session <id>` for the numbers → read the transcript for
specifics → explain *where* the tokens went and *how to cut them* (e.g. "60% was
re-reading big.json ×3 uncached; the system prompt changed mid-session so the cache
missed — use targeted reads"). Interpretation comes from the agent running the skill;
ringleader itself makes no model call.

## Error handling / degradation

- A harness whose directory is absent contributes nothing (skip, no error).
- Malformed / partial records are skipped per-line; a reader never aborts the run on one
  bad record.
- Missing or renamed fields (likely for the less battle-tested OpenCode/Pi layouts)
  degrade to defaults (0 tokens / `None` cost), never a crash.
- Unknown model / provider → cost is omitted (`None`), tokens still counted.
- `rl usage windows` with no Codex data prints a friendly "no Codex window data" state.

## Testing

Hermetic: one small fixture transcript per harness under tmp dirs, with each reader's
file-location patched to tmp. Assert each reader's `UsageRecord` output (including the
per-harness field mapping and dedup), the rollups/bucketing, Codex window parsing, the
session-breakdown math (cache hit-rate, top turns, context growth), and the merge dedup.
Never read real `~/.claude` / `~/.codex` / `~/.local/share/opencode` / `~/.pi`, never
call a model, never touch the network.

## Out of scope (later sub-projects)

- Claude live 5-hour/weekly window capture (a proxy or hook; never persisted to disk).
- Provider usage APIs (Anthropic Admin, OpenAI, OpenRouter) — cross-machine org totals,
  but need admin/org keys ringleader does not hold, and cover API billing, not
  subscriptions.
- Per-tool token attribution (not on disk for any harness).

## Risks / to verify during implementation

- **Codex summing:** confirm `last_token_usage` is the per-turn delta (not cumulative)
  and whether its `output_tokens` already includes `reasoning_output_tokens` — this
  determines how to sum without double-counting.
- **OpenCode/Pi layouts** are less stable than Claude's; the readers must tolerate
  missing/renamed fields and skip cleanly.
- **Cross-machine merge** is intentionally a file-based export + `--merge`, not a sync
  service; dedup keys must be stable across machines.
