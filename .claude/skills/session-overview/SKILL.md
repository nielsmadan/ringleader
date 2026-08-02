---
name: session-overview
description: Use to see all dormant Claude Code work threads across every project at a glance — what each is about, its status, age, and how thin you are spread (live thread count, project spread, blocked and stale counts). Answers "what do I have in flight / abandoned right now?"
---

# session-overview

Shows the curated overview of dormant sessions from the ringleader registry.

## Steps

1. **Check freshness.** Run from any cwd:

   ```bash
   rl overview
   ```

   To scope to one repo/dir (across all its checkouts), add `--project <selector>`
   (repeatable), e.g. `rl overview --project acme-core`.

2. **If the output starts with a staleness/unclassified nudge**, run the
   `session-sync` skill first, then re-run the command above.

3. **Present the overview to the user**, then add a short read of the situation:
   - Call out anything `blocked` (waiting on them).
   - Call out stale live threads (>21 days) as abandon/harvest candidates.
   - If `Live threads` is high and spread across many projects, note the spread
     ("you're juggling N threads across M projects").

## List the repos/dirs with sessions

To answer "which repos/dirs do I have sessions for?" (e.g. to pick a scope for
naming or syncing), run:

```bash
rl overview
```

It prints each git project (and non-git cwd) with total / classified / unnamed
session counts plus a `chk` checkout count, git projects first, most sessions
first. Add `--checkouts` to also list each repo's checkout (cwd) paths.

## Notes
- This is read-only; it never deletes or renames anything.
- Curation actions (naming, harvest, prune) are separate Phase-2 skills.
