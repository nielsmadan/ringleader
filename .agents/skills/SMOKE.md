# Smoke checklist

The `ringleader-*` skills (`.agents/skills/ringleader-{overview,init,triage,cleanup}/`) are
markdown (agent-driven flows over the `rl` CLI), so there's no unit-test harness for them —
`just test` covers `rl` itself. This is a manual run list for exercising the skills end to
end before a release. Each row gives the command(s) to run and what "working" looks like.

Before exercising a skill, confirm it's actually reachable by the agent you're testing in:
run `rl skills install` (or `--yes` to skip the confirmation prompt) to symlink the bundled
skills into `~/.claude/skills` / `~/.agents/skills`, then `rl skills status` to check each
one shows `linked`. `rl skills update` re-syncs after the bundled set changes;
`rl skills uninstall` removes the symlinks this tool created.

Use a throwaway `--registry <path>` (both `rl overview` and `rl triage` accept it) so a
smoke run doesn't touch your real registry state. Scanning still reads real transcripts
from `~/.claude/projects/`, so prefer read-only steps and narrow `--recent` windows —
per `AGENTS.md`, this tool operates on live session data.

| # | Skill / behavior | Command(s) | Expected observable |
|---|---|---|---|
| 0 | `rl skills install`/`status` wire the skills up | `rl skills install --yes`, then `rl skills status` | `install` symlinks the bundled `ringleader-*` skills into `~/.claude/skills` and/or `~/.agents/skills` for each detected agent and reports how many it linked; `status` lists each detected agent's skills dir with every `ringleader-*` skill marked `linked`. |
| 1 | `ringleader-overview` renders and offers triage | `rl overview` (or `--registry <tmp>` against a registry seeded with a session whose `lastActive` is >24h old and not yet triaged), then `rl triage --json` | Overview prints the project rollup with the `⚠ N awaiting triage` header; the >24h-old, undecided session shows up in the `rl triage --json` inbox, and the `ringleader-overview` skill's Step 2 surfaces its count and offers to drop into triage before continuing. |
| 2 | `ringleader-triage` completes a small scope end-to-end | `rl triage --recent 3 --registry <tmp-registry> --json`, then drive the `ringleader-triage` skill against that registry: `rl prune --registry <tmp-registry>` (preview), per-session `rl session set --session <id> --status ... --what ... --name ...` or `rl session delete <id>`, `rl session name --registry <tmp-registry>`, `rl session mark reviewed <id>`, `rl session mark cleaned <project>` | The 3-day inbox empties: `rl triage --recent 3 --registry <tmp-registry> --json` returns `[]` afterward, and `rl overview --registry <tmp-registry>` shows the project's `cleaned` column updated to today. |
| 3a | harvest writes to a clean checkout | Pick a finished session whose repo checkout (`rl overview --checkouts`) has a clean `git status`; run the harvest sub-protocol's extract + synthesize steps, writing doc edits directly into that checkout | `git status` in the checkout shows the new doc edits as uncommitted changes (nothing committed on the user's behalf); `rl session mark harvested <id> --paths <doc paths>` then `rl session archive <id> --summary-file <notes path>` succeed. |
| 3b | harvest falls back to a worktree when dirty | Same as 3a but against a repo whose checkout has uncommitted changes (or no checkout at all) | The skill creates an isolated `git worktree add <tmp dir>` instead of writing into the dirty checkout, reports the worktree path to the user, and the doc edits land there — the original dirty checkout is untouched (`git status` there is unchanged). |
| 4 | `ringleader-init` reports retention, offers to wire skills, and runs a bounded pass | `rl --help` (or `uv run rl --help` from a source checkout) to confirm `rl` resolves, then `rl doctor` | `rl doctor` reports each installed harness's retention setting, including Claude Code's `cleanupPeriodDays` from `~/.claude/settings.json` (flagging it if low/unset); the `ringleader-init` skill offers to run `rl skills install`, then hands off to triage scoped with `--recent 7`, producing a small, finishable first pass rather than the whole backlog. |
| 5 | `ringleader-cleanup` resumes on re-run | Run the `ringleader-cleanup` skill against a registry with a multi-project backlog, stop partway through (after at least one `rl session mark cleaned <project>`), then invoke `ringleader-cleanup` again | The second run's inbox (`rl triage --registry <tmp-registry> --json`, no `--recent`) no longer includes sessions already marked `reviewed`/`harvested`, and projects already marked `cleaned` aren't re-presented — the run picks up from where the first one stopped instead of restarting the whole backlog. |
