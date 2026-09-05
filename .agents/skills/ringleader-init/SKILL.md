---
name: ringleader-init
description: Onboard a machine onto ringleader — verify the CLI, check session-retention settings, and run a bounded first triage pass. Use when the user says "set up ringleader", "get started with ringleader", "onboard", "onboard me", or invokes init.
---

# Init

This skill uses one mechanism your agent provides. `[SELECT]`: use your native
structured-choice tool (Claude Code: `AskUserQuestion`) to get a per-item decision; if you
have none, present a **numbered list** and read back the choices. This skill has no
fan-out step. Shared conventions — calling `rl`, strategic ordering, the harvest
sub-protocol, and the opening/baseline/closeout bracket — are in `references/playbook.md`;
this file only adds the flow.

## Flow

1. **Say what this run will do, then snapshot the baseline.** Follow
   `references/playbook.md` § Opening, baseline, closeout. Tell the user up front that init
   checks the CLI, checks retention settings so sessions aren't garbage-collected, and then
   does one bounded triage pass over the last 7 days — that this pass will delete obvious junk and ask them for a decision per session,
   and that everything older is left for `cleanup`. Then record the baseline numbers before
   anything changes; the closeout in step 7 is measured against them.

2. **Verify `rl` is installed.** Run `rl --help`. If it's not on `PATH`, this is a source
   checkout that hasn't been installed — use `uv run rl` for every command below instead
   (see `references/playbook.md` § Calling `rl`), and mention to the user that
   `just install-local` (or `uv tool install .`) would put `rl` on `PATH` directly.

3. **Wire the skills into this agent.** Run `rl install skills`, which links the bundled
   `ringleader-*` skills into the agent directories it finds. `rl doctor` reports what is
   already wired, so skip this if it says they are. `rl update skills` refreshes them after
   an upgrade and `rl uninstall skills` removes them.

4. **Check retention settings.** Run `rl doctor`. Relay its findings for each installed
   harness. For Claude Code specifically, it reports the `cleanupPeriodDays` setting
   (`~/.claude/settings.json`) that controls how long a dormant session survives before
   Claude's own garbage collection deletes it — if `rl doctor` flags it as low or unset,
   explain the risk and offer to help the user raise it.

5. **Check live-session presence.** Run `rl data presence status`. It reports whether the
   shared status binary (hooklinesinker, bundled with ringleader — **not** a separate Brew
   install) is active and whether ringleader is registered as one of its consumers. If it is
   already set up, say so and move on. If it is not, explain before touching anything:
   activating it writes status hooks into each detected agent's own configuration
   (`~/.claude/settings.json`, `~/.codex/hooks.json`, the OpenCode plugin directory, the Pi
   extension directory, `~/.factory/hooks.json`, `~/.qwen/settings.json`,
   `~/.kimi-code/config.toml`) and registers ringleader alongside any other tool already using
   the binary. Get the user's approval, then run `rl data presence install` for the agents they
   chose — `rl data presence uninstall` later removes only ringleader's registration. If the
   command reports no staged artifact (a source checkout), relay the command it names and
   move on: everything else in ringleader works without it.

6. **Bounded first triage.** Kick off the `triage` skill, scoped to the last 7 days
   (`--recent 7`), so onboarding produces a small, finishable first pass rather than the
   full backlog. Follow that skill's flow as-is; this step is purely a scope hand-off, not
   a separate implementation.

7. **Close out.** Run the closeout from `references/playbook.md` § Opening, baseline,
   closeout: the before → after table against step 1's baseline, what changed (pruned +
   disk reclaimed, classified, kept), what came back **blocked on the user**, and how much
   of the backlog is still untouched — everything older than 7 days, which is what
   `cleanup` is for.

   Then `[SELECT]` the next step rather than stopping on the summary. Offer, recommended
   first: keep going on the next batch of the recent window; run `cleanup` for the full
   backlog; resume one of the blocked sessions (name them); or stop here. Act on whatever
   they pick.
