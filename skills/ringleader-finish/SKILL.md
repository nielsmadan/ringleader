---
name: ringleader-finish
description: Finish the session you are in — harvest anything durable it learned and mark it to be archived when you quit, so it never reaches triage. Use when the user says "finish this session", "I'm done here", "wrap this up", or invokes finish.
---

# Finish

Scope: **this session**. Every other ringleader skill curates sessions that are already
dormant; this one runs inside a live session and captures it while its context is still
here. That context is the whole point — you know things about this session that were
never written to the transcript, and a model reading it later will not.

This skill uses one mechanism your agent provides. `[SELECT]`: use your native
structured-choice tool (Claude Code: `AskUserQuestion`) to confirm; if you have none,
present a **numbered list** and read back the answer. Everything else — calling `rl`, the
harvest sub-protocol — is in `references/playbook.md`.

## Flow

1. **Identify the session.** Run `rl session refresh`, then `rl session whoami`. The
   refresh registers this session if it started since the last scan; without it step 3
   fails after the harvest is already written. If `whoami` reports that this is not a
   recognised agent session, say so and stop — pass the id explicitly to the commands
   below only if the user supplies one.

2. **Confirm the work is finished.** `[SELECT]` between:
   1. **Finished — harvest and archive it.** Durable knowledge came out of this session
      that is not already recorded somewhere else.
   2. **Finished, nothing to keep — just archive it.** Housekeeping, or its result already
      lives in the registry, the repo, or another harvest. Skip step 3.
   3. **Not finished — remind me in N days.** Ask how many, then run
      `rl session snooze <id> --for <days>` and stop.

   Do not offer any other outcome. A session that is neither finished nor worth revisiting
   is a normal triage decision, not a finishing one.

3. **Write the harvest — only if there is one.** The harvest sub-protocol in
   `references/playbook.md` applies to a session that is finished **and** carries knowledge
   worth keeping. Finished does not imply harvestable, so apply that condition rather than
   writing a harvest by default.

   Skip it when this session's output already lives somewhere durable: housekeeping and
   admin runs, anything whose result is the state it already wrote (a triage or cleanup run
   is recorded in the registry and in the harvests it produced), and work whose knowledge
   went into the repo as commits or docs. Re-recording that duplicates it. Note also that a
   harvest is filed against this session's folder, so `ringleader-publish` routes it to that
   checkout's docs — a session *about* your tooling, run from some other project's
   directory, harvests into the wrong repo.

   If it passes, write it from this session's own context, not by re-reading the transcript.
   Cover what was decided and why, what surprised you, and what changed. Omit routine steps.
   Then store it:

   `rl session harvest --current --from-file - <<'EOF' … EOF`

4. **Mark it.** Run `rl session finish --current` — or `rl session finish --current --force`
   on the nothing-to-keep path, since `finish` otherwise refuses a session with no harvest.

5. **Say what happens next.** The session leaves triage immediately. Its transcript is
   archived once it goes quiet — within seconds of quitting if session-end hooks are
   installed (`rl install hooks`), otherwise at the next `rl roundup` (or `rl session
   refresh`); `rl doctor` reports which applies. Either way it will not appear in triage in
   the meantime.
