---
name: session-sync
description: Use to refresh the ringleader registry — scan ~/.claude/projects, then classify each dormant Claude Code session that is new or changed since last sync. Run before session-overview, or whenever the overview reports unclassified threads.
---

# session-sync

Refreshes `registry.json` and classifies dormant sessions. Deterministic scanning is
done by the `rl-sync` CLI; you (the agent) do the classification.

## Steps

1. **Scan and reconcile.** Run from any cwd:

   ```bash
   cd ~/wrksp/incubation/ringleader && .venv/bin/python bin/rl-sync scan
   ```

   To scope to one repo or directory, add `--project <selector>` (repeatable).

   This rewrites `registry.json` and prints JSON: `{pendingCount, total, pending: [...]}`.
   Each `pending` item has `sessionId`, `project`, `customTitle`, `aiTitle`,
   `lastPrompt`, `messageCount`, `gitProject`, `gitBranch`, and `tail` (the last
   turns of the conversation). `gitProject` and `gitBranch` are useful context for
   naming and classifying sessions (e.g. which repo and branch the work was on).

2. **If `pendingCount` is 0**, report "registry up to date (N sessions)" and stop.

3. **Classify each pending session** from its `tail` (and `lastPrompt`). Decide:
   - `status` — exactly one of:
     - `finished` — the work reached a clean conclusion (task done, tests pass,
       question answered, user said thanks / it works).
     - `blocked` — the last turn is a question or error waiting on the user, or the
       assistant asked for a decision and nothing followed.
     - `unfinished` — real work was underway and just stops mid-stream, no clean end.
     - `active` — only if it looks like it was interrupted moments ago (rare for
       dormant sessions); when unsure between active and unfinished, pick `unfinished`.
   - `whatItIs` — one concise line describing the work (≤ ~90 chars).
   - `suggestedName` — kebab-case, ≤ ~40 chars, specific (e.g. `login-button-anim`,
     not `frontend-work`). If `customTitle` is already set, reuse it as the name.
   - `confidence` — 0.0–1.0.

   For large backfills (dozens of pending sessions on the first run), classify in
   batches and consider dispatching parallel subagents (one per batch) to keep it fast.

4. **Write each result back:**

   ```bash
   cd ~/wrksp/incubation/ringleader && .venv/bin/python bin/rl-sync apply --session <sessionId> \
     --status <status> --what "<whatItIs>" --name "<suggestedName>" --confidence <0..1>
   ```

5. **Report** a short summary: how many classified, and the status breakdown.

## Notes
- Never hand-edit `registry.json` or the `.jsonl` transcripts.
- `suggestedName` is a *proposal*. Applying it to the real session is the
  `session-name` skill's job (Phase 2) via native `/rename` / `claude -n`.
- Re-running is cheap: only new or changed (newer `mtime`) sessions reappear as pending.
