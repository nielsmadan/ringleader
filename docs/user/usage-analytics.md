# Usage analytics

Local usage reports work offline from the state your agents already write. Subscription
and OpenRouter reports can fetch provider data or read saved snapshots.

In `rl inbox`, press `7` for Usage. Click Local, Subscription or Token, or cycle through
them with `Tab` and `Shift+Tab`. Local shows token totals and captured quota windows.
Returning to Local restores its completed output. Press `r` to recalculate it.

Subscription and Token open saved reports. Press `r` to fetch and `c` to cancel a run.
While a refresh runs, each provider and report keeps its last displayed data under
**Previous report**, with the original fetch time and identity. Cancellation keeps those
snapshots visible. If a refresh returns a diagnostic without data for a report you've
already seen, the inbox keeps its previous snapshot under **Previous report**.

Token shows up to 30 OpenRouter activity rows in the inbox and indicates when more rows
are available. Run `rl usage token --report activity --cached` to read the latest saved report.
If the inbox says the preview could not be saved, run `rl usage token --report activity --refresh`
to fetch a new full report.

## The basics

```sh
rl usage                          # last 7 days, by project
rl usage --by day                 # day by day
rl usage --by model --all         # all time, by model
rl usage --by session --limit 5   # the five most expensive sessions
rl usage --source codex --days 30
rl usage --breakdown              # add the input/output/cache split
```

`rl usage local` accepts these same flags and produces the same output. Its nested
`rl usage local session`, `rl usage local windows` and `rl usage local export` commands
are explicit local aliases. All of these commands work offline without provider credentials.

Covers **Claude Code, Codex, Droid, OpenCode and Pi**. Default window is the trailing 7 days
grouped by project; `--all`, `--days N` or `--since`/`--until` widen it. `--archived` also counts
the transcripts `rl session archive` moved out of the agents' own stores.

## One session

```sh
rl usage session <id>
```

Turn count, the full token split, cache hit rate, the top turns, and context growth. Totals
include subagent turns; the top turns and context growth cover the main thread only, and say so
when the session has subagents (`main_turns` in the JSON). An id
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

## Captured subscription windows

```sh
rl usage windows
```

5-hour and weekly consumption.

- **Codex** needs no setup — it persists its rate limits to disk.
- **Claude** needs a one-time `rl install statusline`. Claude Code (**v2.1.80+, Pro/Max only**)
  provides `rate_limits` through the statusLine hook payload after the first API response
  in a session.

Each window can independently be present or absent. Only snapshots whose window hasn't reset are
reported as current; expired ones are listed under `codex_expired` / `claude_expired` in JSON
and "no current snapshot" in text. OpenCode and Pi have no subscription-window concept, and
droid's limits live only behind Factory's API — nothing on disk to read, so `rl usage windows`
does not cover it.

- **Freshness is bounded by usage.** Claude's local numbers are only as fresh as the last
  statusLine render, with a 10-minute TTL. A stale capture reports "no current snapshot".
- **Multi-account ambiguity.** The statusLine payload carries no account identity. With several
  Claude accounts on one machine, the capture reflects whichever rendered the statusLine last.

## On-demand subscription quotas

```sh
rl usage subscription --cached
rl usage subscription --source claude --refresh
rl usage subscription --source codex --refresh --json
```

The report shows each provider's quota windows with utilization and reset time when available.
Claude model-specific windows and Codex metered buckets have their own labels. A missing value
displays as unknown, and an elapsed reset time displays as expired.

| Provider | Setup |
| --- | --- |
| Claude environment auth (default) | Supply `CLAUDE_CODE_OAUTH_TOKEN` with the `user:profile` OAuth scope. |
| Claude agent auth | Set `rl config set usage.claude_auth agent` to reuse the selected signed-in profile. Sign in again in Claude Code if the profile credential expires. |
| Codex agent auth (opt-in) | Set `rl config set usage.codex_auth agent` and use Codex CLI 0.170.0 or newer with an existing login. |

Claude agent auth uses the profile at `CLAUDE_SECURESTORAGE_CONFIG_DIR`, then
`CLAUDE_CONFIG_DIR`, then `~/.claude`. On macOS, the default login profile can use Keychain
when both overrides are unset and the OS permits prompt-free access. An overridden profile
uses its own `.credentials.json`. If Keychain denies access, choose a profile with a usable
credentials file or return to `rl config set usage.claude_auth env` and supply the scoped token.

Codex starts its local app-server to query account quota. Startup can load Codex plugins
and configuration, and Codex can persist its normal auth refresh. `rl config set
usage.codex_auth disabled` turns off Codex usage access.

`--source` can be repeated to request more than one provider. Without it, both providers
are reported and one successful provider can make missing setup for the other informational.

## OpenRouter

```sh
rl usage token --cached
rl usage token --report key
rl usage token --report activity --days 7 --json
rl usage token --report credits --refresh
```

- **Key** uses `OPENROUTER_API_KEY` for that API key's spend, external BYOK spend, and
  spending cap. It shows all-time, daily, weekly, and monthly figures and the key's
  remaining cap and reset when available.
- **Activity** uses `OPENROUTER_MANAGEMENT_KEY` for endpoint activity visible to the
  management key's account or workspace. It defaults to the last 30 completed UTC days.
  `--days 1..30` narrows that period. Each row keeps OpenRouter and BYOK spend and the
  reported prompt, completion, cached, and reasoning counters, which can overlap.
- **Credits** uses `OPENROUTER_MANAGEMENT_KEY` for the authenticated user's purchased
  and used credits. Balance is purchased minus used and can be negative.

Supply the keys through the environment. Each report has its own scope and the two
credentials may belong to different accounts. The command presents separate figures for
each scope.

The default and `--report all` show all three reports. Missing credentials are
informational if another report succeeds. Selecting a single report requires its key
for online use. `--source openrouter` selects the only supported provider.

## Saved reports, refresh, and JSON

`--cached` reads the last fetched snapshot for each scope without looking up
credentials, starting app-server, or contacting a provider. The report shows when each
snapshot was fetched. Activity looks up the requested day count and shows the original dates.
A cold cache reports `uncached` and exits 0.

Online Claude and OpenRouter calls reuse a fresh snapshot for the same credential and query.
Set `usage.cache_seconds` to change the default of 60 seconds (range 0–3600). Each online
Codex call retrieves quota because account identification depends on the app-server response.
If `accountId` is missing or empty, the saved quota snapshot has an unknown identity. `--refresh`
bypasses cache freshness and honors provider retry times. If a failed fetch can attach an
older snapshot for the same verified scope, the snapshot keeps its original timestamp and
is marked stale.

Remote commands exit 1 on a report error, a cache read error, missing credentials for an
explicit subscription source or single OpenRouter report, or when an online invocation has
no successful reports.
Any independently successful results still appear. A fresh result can carry a warning if its
cache write failed. A successful refresh that repairs an invalid cache reports `cache_repaired`.
Invalid arguments exit 2.

Remote `--json` returns a standalone version 1 document with `kind` (`subscription` or
`token`) and a `results` array. Each result has `provider`, `scope`, opaque `identity`,
`status`, `fetched_at`, `cached`, `stale`, `data` and `diagnostic`. Status is `ok`,
`not_configured`, `uncached` or `error`. Unknown data uses `null`. See the
[technical reference](../tech/usage.md#remote-reporting-foundations) for scope-specific
schemas and fixture examples.

## Combining machines

```sh
rl usage export --out usage-laptop.json     # on each machine
rl usage --merge <dir>                      # combined report
```

File-based, no sync service. Records carry a machine id and are de-duplicated on merge.
Exports from this version need this version (or newer) to merge: every record now carries
`cache_write_1h` and `nested`, and an older rl drops every record of such an export. When merging
across machines, upgrade them all, or the newer machine shows zero usage on the older one.

To see where sessions went wrong rather than where tokens went, use `rl analyze`
([analysis](../features/analysis.md)).

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

## `rl data tokens`

This runs the local usage summary with `--source claude` preselected. It accepts the local
summary flags. For example, `rl data tokens --source codex --json` reports both Claude Code
and Codex.
