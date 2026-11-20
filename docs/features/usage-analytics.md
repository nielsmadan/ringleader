# See where the tokens went

> **Problem:** a session cost far more than it should have and there is no way to see why, or
> how close a subscription window is to its limit, without leaving the terminal.

## Overview

`rl usage` and `rl usage local` report token spend across **Claude Code, Codex, Droid, OpenCode
and Pi**, read offline from local transcripts, sidecars and database rows.

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

**Droid records carry no cost at all.** Factory routes requests and bills its own credits, so the
model vendor's list price says nothing about what a droid session cost — `pricing.estimate()`
returns `None` for the `factory` provider rather than a confident wrong number. `rl usage
windows` likewise does not cover droid: those figures exist only behind Factory's API.

## Captured subscription windows

`rl usage windows` reports 5-hour and weekly consumption.

- **Codex** writes its window state to disk — nothing to set up.
- **Claude Code** provides rate limits to the status line. `rl install statusline` captures
  them for Claude Code **v2.1.80+** on Pro/Max after the first API response in a session.

## On-demand reports

- `rl usage subscription` reports Claude and Codex quota windows. Claude defaults to an
  environment OAuth token with `user:profile`. Reusing either agent's sign-in requires the
  corresponding `usage.claude_auth agent` or `usage.codex_auth agent` setting.
- `rl usage token` reports OpenRouter API key spend, activity grouped by endpoint for up to
  30 completed UTC days, and user credits. Each report applies to the scope visible to its
  environment key. Local token estimates and remote quota, spend and credit figures have
  their own units and scopes.
- `--cached` shows the last fetched scope offline. A saved snapshot carries its timestamp
  and stale status. `--refresh` requests new data and honors retry times. The CLI may
  return successful reports alongside other scopes' errors.
- The inbox Usage screen (`7`) starts on Local. Click or cycle with `Tab` and `Shift+Tab`.
  Local restores its completed output on return. Subscription and Token open saved reports.
  Press `r` to refresh and `c` to cancel. A previous snapshot stays available for each scope
  while refreshing, after cancellation, or when that scope fails to refresh. The Token view
  shows up to 30 activity rows. The palette's remote usage commands open cached reports.

## Archived sessions and across machines

```sh
rl usage --archived                 # include archived Claude, Codex + Droid transcripts
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

- **`rl data tokens` runs the local usage summary with Claude Code preselected.** It accepts
  the summary flags, including additional `--source` values.
- **Captured Claude windows** require the status-line capture and a session's first API
  response. The on-demand subscription path uses OAuth.
- **Reading is I/O-bound over local agent state**, so all-time reports on a large history are slow.
- **OpenCode records are per assistant response.** SQLite `message.data` retains the timestamp,
  model, cost and token buckets, while the owning `session` supplies project identity.
- **Droid records are per session, not per turn**, because droid records no per-turn counts —
  only sidecar totals. Each record is stamped at the transcript's mtime, so a long session's
  whole spend lands on the day it was last touched, and `--by day` mixes per-turn sources with
  per-session lumps. `--by model` has the same shape of approximation: the whole session is
  attributed to whichever model the router last chose.

## Related

- [tech/usage.md](../tech/usage.md) · [user/usage-analytics.md](../user/usage-analytics.md)
  · [reference/provider-usage.md](../reference/provider-usage.md)
