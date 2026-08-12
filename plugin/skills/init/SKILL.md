---
name: init
description: Onboard a machine onto ringleader — verify the CLI, check session-retention settings, and run a bounded first triage pass. Use when the user says "set up ringleader", "get started with ringleader", "onboard", "onboard me", or invokes init.
---

# Init

This is the Claude wrapper: map `[SELECT]` to the `AskUserQuestion` tool; there is no
`[FAN-OUT]` in this skill. Shared conventions — calling `rl`, strategic ordering, the
harvest sub-protocol — are in `plugin/PLAYBOOK.md`; this file only adds the flow.

## Flow

1. **Verify `rl` is installed.** Run `rl --help`. If it's not on `PATH`, this is a source
   checkout that hasn't been installed — use `uv run rl` for every command below instead
   (see `PLAYBOOK.md` § Calling `rl`), and mention to the user that `just install-local`
   (or `uv tool install .`) would put `rl` on `PATH` directly.

2. **Check retention settings.** Run `rl doctor`. Relay its findings for each installed
   harness. For Claude Code specifically, it reports the `cleanupPeriodDays` setting
   (`~/.claude/settings.json`) that controls how long a dormant session survives before
   Claude's own garbage collection deletes it — if `rl doctor` flags it as low or unset,
   explain the risk and offer to help the user raise it.

3. **Bounded first triage.** Kick off the `triage` skill, scoped to the last 7 days
   (`--recent 7`), so onboarding produces a small, finishable first pass rather than the
   full backlog. Follow that skill's flow as-is; this step is purely a scope hand-off, not
   a separate implementation.

4. **Point at the backlog.** Once the bounded pass is done, tell the user everything older
   than 7 days is still pending and that the `cleanup` skill is what to run for the full
   backlog, whenever they're ready for it.
