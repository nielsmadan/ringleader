---
name: ringleader-init
description: Onboard a machine onto ringleader — verify the CLI, check session-retention settings, and run a bounded first triage pass. Use when the user says "set up ringleader", "get started with ringleader", "onboard", "onboard me", or invokes init.
---

# Init

This skill uses one mechanism your agent provides. `[SELECT]`: use your native
structured-choice tool (Claude Code: `AskUserQuestion`) to get a per-item decision; if you
have none, present a **numbered list** and read back the choices. This skill has no
fan-out step. Shared conventions — calling `rl`, strategic ordering, the harvest
sub-protocol — are in `references/playbook.md`; this file only adds the flow.

## Flow

1. **Verify `rl` is installed.** Run `rl --help`. If it's not on `PATH`, this is a source
   checkout that hasn't been installed — use `uv run rl` for every command below instead
   (see `references/playbook.md` § Calling `rl`), and mention to the user that
   `just install-local` (or `uv tool install .`) would put `rl` on `PATH` directly.

2. **Check retention settings.** Run `rl doctor`. Relay its findings for each installed
   harness. For Claude Code specifically, it reports the `cleanupPeriodDays` setting
   (`~/.claude/settings.json`) that controls how long a dormant session survives before
   Claude's own garbage collection deletes it — if `rl doctor` flags it as low or unset,
   explain the risk and offer to help the user raise it.

3. **Offer to wire the skills into the user's agents.** `[SELECT]`: ask whether to install
   the ringleader skills for every agent detected on this machine (Claude Code, Codex,
   OpenCode, Pi), so `overview`/`triage`/`cleanup`/`init` are available directly instead of
   through this bootstrap flow. If yes, run `rl skills install --yes`. Mention `rl skills
   status` to check what's linked, and `rl skills update` to re-sync later if the bundled
   skill set changes.

4. **Bounded first triage.** Kick off the `triage` skill, scoped to the last 7 days
   (`--recent 7`), so onboarding produces a small, finishable first pass rather than the
   full backlog. Follow that skill's flow as-is; this step is purely a scope hand-off, not
   a separate implementation.

5. **Point at the backlog.** Once the bounded pass is done, tell the user everything older
   than 7 days is still pending and that the `cleanup` skill is what to run for the full
   backlog, whenever they're ready for it.
