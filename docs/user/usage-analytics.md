# Usage analytics

Where your tokens went, read from the local state your agents already write. No API
call, no credential, works offline.

## The basics

```sh
rl usage                          # last 7 days, by project
rl usage --by day                 # day by day
rl usage --by model --all         # all time, by model
rl usage --by session --limit 5   # the five most expensive sessions
rl usage --source codex --days 30
rl usage --breakdown              # add the input/output/cache split
```

Covers **Claude Code, Codex, Droid, OpenCode and Pi**. Default window is the trailing 7 days
grouped by project; `--all`, `--days N` or `--since`/`--until` widen it. `--archived` also counts
the transcripts `rl session archive` moved out of the agents' own stores.

## One session

```sh
rl usage session <id>
```

Turn count, the full token split, cache hit rate, the top turns, and context growth. An id
prefix matching several sessions is refused rather than silently merged. OpenCode detail uses the
per-response token, model, cost and timestamp data in its database, so day and model grouping do
not shift when an old session is resumed. **Droid has no per-turn detail to show** — its counts
are a single per-session total — so a droid session reports one "turn" holding the whole spend.

For advice rather than numbers, the `ringleader-usage` skill surveys usage, drills into the
heavy sessions, reads the transcript around the expensive turns, and tells you what to do
differently. Read-only.

## Costs are estimates

Tokens are the primary unit. A cost column appears only where cost is known — on disk for
OpenCode and Pi, a list-price estimate for Claude. **Codex publishes no prices**, and **droid is
never priced at all**: Factory routes your requests and bills its own credits, so the model
vendor's list price would be a confident wrong number rather than an estimate. A scope mixing
either with a priced source reports a floor, flagged `cost_partial` in JSON and `(partial: …)` in
text. A scope with nothing priced reports `null`, never `0`.

## Subscription windows

```sh
rl usage windows
```

5-hour and weekly consumption.

- **Codex** needs no setup — it persists its rate limits to disk.
- **Claude** needs a one-time `rl install statusline`. Claude Code (**v2.1.80+, Pro/Max only**)
  exposes `rate_limits` *only* through the statusLine hook payload, never to disk, and only
  after the first API response in a session.

Each window can independently be present or absent. Only snapshots whose window hasn't reset are
reported as current; expired ones are listed under `codex_expired` / `claude_expired` in JSON
and "no current snapshot" in text. OpenCode and Pi have no subscription-window concept, and
droid's limits live only behind Factory's API — nothing on disk to read, so `rl usage windows`
does not cover it.

### Two limitations worth knowing

- **Freshness is bounded by usage.** Claude's numbers are only as fresh as the last statusLine
  render (capped at a 10-minute TTL); there's no way to poll for a fresh reading on demand. A
  stale capture reports "no current snapshot", not a stale percentage.
- **Multi-account ambiguity.** The statusLine payload carries no account identity. With several
  Claude accounts on one machine, the capture reflects whichever rendered the statusLine last,
  and there's no way to tell which — a reading can be wrong and look completely normal.

## Combining machines

```sh
rl usage export --out usage-laptop.json     # on each machine
rl usage --merge <dir>                      # combined report
```

File-based, no sync service. Records carry a machine id and are de-duplicated on merge.
Exports from this version need this version (or newer) to merge; an older rl drops every record of
such an export.

## Known reporting artifacts

One quirk in the **project** column. It does not double-count tokens — the totals are right, only
the grouping splits:

- Where a project name is recovered from an encoded directory name rather than a recorded cwd, a
  directory containing a hyphen can't be told apart from a nested path — `foo-bar` and `foo/bar`
  encode identically — so one project can appear as two rows.

OpenCode projects come from the database's `session.project_id` → `project.worktree` join; the
opaque id is never reconstructed from a path.

Claude subagent transcripts (including workflow subagents) and pi nested runs are counted too, credited to the session that spawned them, so those totals are higher
than before. Codex totals can be lower: a forked or spawned rollout replays its parent's history,
and only the rollout's own token counts are counted. Codex records now carry the model from the
rollout's `turn_context`.

Claude cost prices one-hour cache writes at 2x the input price and five-minute writes at 1.25x,
so cost estimates for sessions using the one-hour cache are higher than before.

OpenCode usage includes child/sub-agent session rows because their token totals are separate from
the parent. Those rows do not appear in session overview or triage, which only curate top-level
resumable sessions. Droid counts subagent spend the same way: every droid thread reports its own
total, top-level or subagent. Droid's sidecars also record an inclusive figure covering a thread's
whole subtree, which ringleader ignores — archiving a top-level session separates it from its
subagents, and the inclusive figure would then count their tokens a second time.

**Droid counts are per session, not per turn.** Droid records no per-turn numbers, only a total
per session, so a droid session's entire spend is stamped at the time the transcript was last
touched. `--by day` therefore mixes per-turn sources with per-session lumps, and `--by model`
credits a whole auto-routed session to whichever model the router last chose. The totals are
right; the *grouping* is an approximation.

## `rl data tokens` is not the same command

It's `rl usage` pinned to `--source claude`, accepting the same flags otherwise.
