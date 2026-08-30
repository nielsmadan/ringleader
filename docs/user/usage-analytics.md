# Usage analytics

Where your tokens went, read from the transcripts your agents already write locally. No API
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

Covers **Claude Code, Codex, OpenCode and Pi**. Default window is the trailing 7 days grouped by
project; `--all`, `--days N` or `--since`/`--until` widen it. `--archived` also counts the
transcripts `rl session archive` moved out of `~/.claude/projects`.

## One session

```sh
rl usage session <id>
```

Turn count, the full token split, cache hit rate, the top turns, and context growth. An id
prefix matching several sessions is refused rather than silently merged.

For advice rather than numbers, the `ringleader-usage` skill surveys usage, drills into the
heavy sessions, reads the transcript around the expensive turns, and tells you what to do
differently. Read-only.

## Costs are estimates

Tokens are the primary unit. A cost column appears only where cost is known — on disk for
OpenCode and Pi, a list-price estimate for Claude. **Codex publishes no prices**, so a scope
mixing it with a priced source reports a floor, flagged `cost_partial` in JSON and `(partial: …)`
in text. A scope with nothing priced reports `null`, never `0`.

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
and "no current snapshot" in text. OpenCode and Pi have no subscription-window concept.

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

## Known reporting artifacts

Two quirks in the **project** column. Neither double-counts tokens — the totals are right, only
the grouping splits:

- A harness that records no working directory (OpenCode) groups under `(unknown)`.
- Where a project name is recovered from an encoded directory name rather than a recorded cwd, a
  directory containing a hyphen can't be told apart from a nested path — `foo-bar` and `foo/bar`
  encode identically — so one project can appear as two rows.

## `rl data tokens` is not the same command

It's `rl usage` pinned to `--source claude`, accepting the same flags otherwise.
