# See where the tokens went

> **Problem:** a session cost far more than it should have and there is no way to see why, or
> how close a subscription window is to its limit, without leaving the terminal.

## Overview

`rl usage` reports token spend across **Claude Code, Codex, OpenCode and Pi**, read from the
transcript files or database rows each already writes locally. No API call, no credential, works
offline.

```sh
rl usage                          # last 7 days, by project
rl usage --by day --breakdown     # day-by-day, with per-model detail
rl usage --by model --all         # all time, grouped by model
rl usage --source codex --days 30
rl usage session <id>             # where one session's tokens went
rl usage windows                  # 5h / weekly subscription limits
```

`--by` accepts `day`, `model`, `project`, `session`, `source`. Default window is **7 days** —
`--all`, `--days N`, or `--since`/`--until` widen it.

## Four token counts, not one

Input, cache-read, cache-write and output are tracked separately, because cache reads and writes
price very differently and a session that looks expensive by total is often just cache-heavy.
Cost figures come from a static price table, so they are estimates and go stale when a provider
reprices; an unrecognized model reports no cost rather than a wrong one.

## Subscription windows

`rl usage windows` reports 5-hour and weekly consumption.

- **Codex** writes its window state to disk — nothing to set up.
- **Claude Code** does not expose it. `rl install statusline` wires a snippet into
  `statusLine.command` that captures the limits Claude Code passes the status line. Requires
  Claude Code **v2.1.80+**, Pro/Max only, and only populates after the first API response in a
  session.

## Archived sessions and across machines

```sh
rl usage --archived                 # include archived Claude + Codex transcripts
rl usage --archived --source codex  # only archived Codex sessions
rl usage export --out usage-laptop.json    # on each machine
rl usage --merge <dir>                     # combined report
```

`--archived` merges the archive (`~/.local/share/ringleader/archive`) into the live
report and de-duplicates any session that exists in both trees. Records are tagged
with a machine id and de-duplicated on merge.

## Key entry points

- `usage/aggregate.py` — `READERS` (the valid `--source` values), `rollup()`.
- `usage/pricing.py` — the price table and `estimate()`.
- `usage/claude_limits.py` — the status-line capture reader.
- `usage/transfer.py` — export, load, dedupe.

## Gotchas

- **`rl data tokens` is not the same command.** It pins `--source claude` while accepting the
  same flags.
- **Claude windows report nothing until the status line is installed** *and* a session has had
  its first API response.
- **Reading is I/O-bound over local agent state**, so all-time reports on a large history are slow.
- **OpenCode records are per assistant response.** SQLite `message.data` retains the timestamp,
  model, cost and token buckets, while the owning `session` supplies project identity.

## Related

- [tech/usage.md](../tech/usage.md) · [user/usage-analytics.md](../user/usage-analytics.md)
