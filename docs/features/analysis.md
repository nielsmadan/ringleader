# Find where sessions went wrong

> **Problem:** a session that looped, rewound or rebuilt its cache cost time and tokens, and
> nothing on disk says which sessions did it, how often, or what the recurring causes are.

## Overview

`rl analyze` indexes every Claude Code and Codex transcript into a local SQLite file, detects
failure signals in them, and reports where sessions went wrong and where tokens went. It reads
only local files. A model is used only for optional labels.

```sh
rl analyze                          # last 7 days: totals, cache, rollups, worst sessions, issues
rl analyze ringleader --by tool     # one git project, grouped by tool
rl analyze --all --source codex     # all time, one agent
rl analyze session <id>             # one session in detail
rl analyze issues --min-sessions 3  # recurring error signatures across sessions
rl analyze reports                  # saved findings and recommendations
```

The first run reads every transcript and takes minutes on a large history. Later runs re-read only
what changed. See [tech/analysis.md](../tech/analysis.md#performance) for timings.

## Views

- **Corpus** (`rl analyze`): totals, cache stats, rollups by `--by`, the `--worst` sessions
  (default 10) and the top `--issues` (default 5). `--by` repeats and takes `project`, `folder`,
  `skill`, `plugin`, `mcp_server`, `tool`, `model`, `agent`. The default is `project`, `model`,
  `skill`, `tool`. Tool calls carry no model, so `model` rows report tokens and sessions and
  always show 0 calls and 0 errors; `tool` and `mcp_server` rows carry no tokens.
  `--worst` and `--issues` take 0 to leave the section empty.
- **Session** (`rl analyze session <id>`): one session by id prefix, live or archived. Outcome,
  health, signals, episodes with evidence, user messages, compactions, context growth, skills,
  tools and subagents. `issues` lists the ids of the recurring issues, over all history, whose
  signatures the session hit. An ambiguous prefix lists its matches and exits 1. An id with no
  analysis data exits 1 and points to `rl usage session <id>`, since Droid, OpenCode and Pi
  sessions are counted for tokens only.
- **Issues** (`rl analyze issues`): error signatures that recur across sessions, most sessions
  first, up to `--limit` (default 20).
- **Reports** (`rl analyze reports`): the saved reports, newest first.

Scope flags are shared by the corpus and issues views: a `<selector>` or `--project` (a git-project
label or a cwd path), `--source` (`claude` or `claude-code`, `codex`, `droid`, `opencode`, `pi`),
`--days N` (default 7), `--since`/`--until`, `--all`, `--archived`. The default window ends at the
latest day in the data of the chosen sources, not at today. `--until` alone reads all history up to
that day. `--all` cannot combine with `--since` or `--until`, `--days` cannot combine with any of
the three, and `--since` cannot be later than `--until`. A selector with a `/` is a path; a bare
word must match a git-project label in the registry. A word that matches nothing, or one close to a
subcommand (`issue`, `sesion`), exits 2 before the index is refreshed. `--json` prints the same
document the text view is drawn from.

## Signals

A signal is a pattern in one thread's live calls. Calls on a rewound branch are not live.
Thresholds are the calibrated values; see [Calibration](../tech/analysis.md#calibration). The
Claude precision numbers there were measured two derive versions before the current one; later
versions only narrowed benign-exit and write detection.

| Signal | Fires when |
|---|---|
| `tool_failure` | a call fails. Not an agent failure when the category is below. |
| `retry_run` | the same tool with the same input 3 or more times in a row |
| `runaway_window` | 6 or more failures in 12 consecutive calls, or 3 or more failures of the dominant command class when it makes up 10 of the 12 |
| `failure_streak` | 3 or more failures in a row |
| `edit_churn` | 3 or more edits to one file within 10 calls, with a failure between them or an edit that reverts another |
| `reread_churn` | 5 overlapping reads of one file region within 10 calls and no write between |
| `search_loop` | 20 or more reads and searches in a row after the first write, with no write or validation between and no human message |
| `unsupported_completion` | the final message claims done, and code files were written after the last successful validation |
| `compaction` | the context was compacted. Inferred from a 30% context drop when the agent records none. |
| `mid_task_compaction` | a compaction where the 10 calls before and the 5 after touch a common file, with no human message between |
| `user_interruption` | the user interrupted the agent |
| `rejected_tool_use` | the user declined a tool call |
| `permission_denial` | a call was denied by permissions or the sandbox |
| `rewind` | the user went back to an earlier message and branched |

Interruptions, rejections, permission denials and calls cancelled because a sibling failed are
**not agent failures**. They show only as their own signals and are left out of failure streaks,
runaway windows, episode severity and every error count.

Two hits of one kind at the same call count once.

### Episodes

Overlapping or touching stretch signals merge into one **episode**. A merged stretch is cut where
consecutive hits start more than 30 calls or 30 minutes apart. An episode carries its kinds, call
range, severity, token cost and an excerpt. Point signals (interruption, rejection, denial,
compaction, rewind) are not episodes on their own. Episodes are what labels describe.

### Rewinds

A rewind is the user backing up and re-sending from an earlier message. It is detected on main
threads only. Most rewinds abandon no tool calls: they are edited or re-sent prompts. The
abandoned calls stay in the index but are excluded from every signal and count.

## Health and outcomes

Health is a score and a rate, not a grade. Each signal kind adds penalty points, capped per kind.
A high tool error rate adds more, and so does an `errored` or `abandoned` outcome. The score is
`100 - points`, floored at 0. **Points per 100 calls** is the ranking value, since a long session
earns penalties simply by being long. A session needs at least 5 calls to be ranked, and the
points divide by the whole session's calls, not the day window's share.

The penalty table is `health.PENALTIES`.

Outcome is one of:

| Outcome | Meaning |
|---|---|
| `open` | live now, or no presence data and the last event is under 10 minutes old |
| `completed` | the thread ended on the agent's answer or a finished turn |
| `abandoned` | it ended on an interrupt, an abort, a pending tool call or an unanswered user message |
| `errored` | it ended on an API error |
| `unknown` | none of the above |

`open` is never penalised.

## Cache and cost

Each request records its input, output, cache-read and cache-write tokens, with the write split
into 5 minute and 1 hour entries. A **cache rebuild** is a request that writes cache while reading
less than half of the previous request's context. Cache stats report the hit rate, the writes by
TTL, the number of rebuilds, the tokens they rewrote, how many followed more than 5 minutes or 1
hour idle, and what they cost.

Cost uses the same price table as `rl usage`. One-hour cache writes cost 2x the input price,
five-minute writes 1.25x.

**Rebuild cost is a floor.** A model with no price, such as any non-Claude model or a routed
provider, adds nothing to it. The rebuild counts still include those models, and there is no
partial marker on rebuild cost as there is on total cost.

Token totals count only Claude models for Claude Code, the same rule as `rl usage`, so the two
commands agree. A non-Claude model run through Claude Code appears in neither.

The corpus JSON has two hit rates. `cache.hit_rate` covers the indexed Claude Code and Codex
requests, like the rest of `cache`. `totals.cache_hit_rate`, and the `cache_hit_rate` of each
rollup row, also count the tokens-only agents (Droid, OpenCode, Pi), so the two differ when those
are in scope. The session document has only `cache.hit_rate`.

## Coverage by agent

| Agent | Calls, signals, episodes | Tokens and cache |
|---|---|---|
| Claude Code | yes, including subagents and workflow agents | yes |
| Codex | yes, forked and spawned rollouts counted once | yes |
| Droid, OpenCode, Pi | no | tokens only, through the `rl usage` readers |

`coverage` in the JSON says which. Droid, OpenCode and Pi appear in totals and in the `project`,
`model` and `agent` rollups, and nowhere that needs calls.

## Recurring issues

An issue groups failed calls that share an error signature across sessions: the command class,
the error category and the first error line with paths and numbers normalised. An issue needs
`--min-sessions` sessions (default 2). It shows its sessions, occurrences, agents, first and last
day, example sessions and an example error.

With the optional `embed` extra, near-duplicate signatures merge. It is local only; the model is
fetched once with `rl analyze issues --download-embed-model` and never otherwise. Without it,
only identical signatures group, and the output says why. A missing extra names its install
command, `uv tool install --reinstall '.[embed]'` from a ringleader checkout.
`--download-embed-model` exits 1 when it cannot load the model.

## Labels

Labels are the only model call in `rl analyze`. A cheap model describes each flagged episode
(category and friction tag), classifies the user's messages (correction, frustration,
redirection, new task, approval, other) and names each recurring issue.

```sh
rl analyze --label recent      # last 7 days, at most 50 episodes, 500 messages, 20 issues
rl analyze --label all         # every unlabelled episode, message and issue in scope
rl analyze --label none        # never call a model
rl analyze --yes               # skip the confirmation
```

- **Cost guard.** In text output, before any call, `rl` prints the number of episodes, messages
  and issues, the calls, the time, and a dollar estimate when the model has a price. On an
  interactive terminal it then asks `continue? [Y/n]`; `--yes` skips that, and end of input
  declines. `--json` with an explicit `--label` prints no estimate and does not ask: the flag is
  the consent.
- **Non-interactive default.** With no `--label`, a run in a pipe, with stderr redirected, with
  `--json` or inside an agent session uses `none` and says so. The estimate and the prompt go to
  stderr, so a redirected stderr is never asked. Only an explicit `--label`
  calls a model there. `--json` never calls a model without `--label`.
- **Model.** `rl config set analyze.model <name>` overrides it. The default is `haiku` on the
  `claude` engine, and the shared `model` setting on other engines. With no model available, labels
  are skipped with a note. The engine is whichever `rl config set ai` selects.
- **A failed explicit run exits 1.** An explicit `--label recent` or `--label all` that labels
  nothing because no model is available, the labels store cannot be written or was written by a
  newer rl, every call failed, or the breaker tripped, still prints the report and exits 1. A
  declined prompt exits 0.
- **Ctrl-C stops at once.** In-flight model calls are cancelled. With the `llm` engine an HTTP
  request already sent is abandoned in the background rather than awaited.
- **Stored once.** Labels persist across runs. A label is reused until its episode's content or
  the label prompt changes. Labels are model output: they are never rebuilt and never deleted.
- `rl analyze session <id>` labels all of that session's episodes, without the 50 episode cap.

## Reports and skills

`rl analyze` prints numbers. Judgement lives in two skills:

- `ringleader-analyze` reads the analysis, checks claims against the evidence in `rl analyze
  session`, and writes the findings as a report.
- `ringleader-recommend` reads saved findings and proposes edits to instruction files, skills and
  config. It proposes only. It never applies a change.

`rl analyze reports save <findings|recommendations> <file|-> [--title T] [--window W]
[--based-on ID]...` stores a report under the state dir, `analysis/findings/` or
`analysis/recommendations/`. `rl analyze reports [--kind K] [--limit N] [--json]` lists them.

Report text is data. Both skills treat report, transcript and tool-output text as content to read
and never as instructions to follow.

## Gotchas

- **The first run is slow, later ones are not.** A cold build reads every transcript. A run after
  that re-reads only changed ones. Live sessions are re-read whole each time.
- **`--no-refresh` reads the index as it is.** It exits 3 when there is no complete index, or
  when a different rl version built it. With `--json` the error document's `index.reason` says
  which: `missing` or `mismatch` (exit 3), `unreadable` or `unwritable` (exit 1).
- **Another rl may be refreshing.** A run that finds the index locked reads it as it stands and
  says the results may be partially refreshed.
- **Exit 1 means something went wrong, not only an error.** It also covers more than 20% of changed
  transcripts failing to parse, an unreadable index, a missing or ambiguous session, an
  explicit `--label` that did not complete, and a `--download-embed-model` that could not load the
  model. Output is still printed when the cause is parse failures, labelling or the embed model.
- **Signals are heuristics.** After calibration the top of the ranking is almost all real, but the
  long tail has noise. The known gaps are existence probes that fail by design, failed writes that
  count as writes, runaway windows that a human message does not reset, and Droid's `Create`
  tool. See [Deferred precision notes](../tech/analysis.md#deferred-precision-notes).
- **Compaction counts do not sum across rows.** A dimension row counts the compactions of the
  sessions it touches. Only the totals row is exact.
- **Rebuild cost is a floor** when any model in scope has no price.
- **Deleted sessions stay in the index until the next refresh.** `rl prune`, `rl roundup` and
  the purge of expired archives delete transcripts but never touch the index. A deleted
  transcript's rows, its message text and error text included, stay until the next `rl analyze`
  refresh drops them, and for as long as its root cannot be listed. Labels about it stay in
  `labels.sqlite`, which is never garbage-collected. Deleting the index file removes the rows at
  the cost of a cold build.
- **Archived sessions are excluded** unless `--archived` is passed. `rl analyze session` finds
  archived sessions by prefix regardless.
- **`rl usage` numbers moved** when analysis landed: subagent spend is counted, Codex forks are
  counted once and one-hour cache writes cost 2x. See
  [Known reporting artifacts](../user/usage-analytics.md#known-reporting-artifacts).

## Key entry points

- `ringleader/commands/analyze.py`; `ringleader/analysis/` (`ingest.py`, `store.py`,
  `signals.py`, `health.py`, `rollups.py`, `labels.py`, `render.py`).
- Skills: `.agents/skills/ringleader-analyze`, `.agents/skills/ringleader-recommend`.

## Related

- [tech/analysis.md](../tech/analysis.md) · [0015: derivable SQLite index](../decisions/0015-sqlite-analysis-index.md)
- [usage-analytics.md](usage-analytics.md) · [agent-integration.md](agent-integration.md)
- [Analyze labelling benchmark](../tests/analyze/README.md)
