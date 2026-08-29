# Finishing a session from inside it

## Problem

Today a session is curated the day after it ends. You quit, and the transcript sits in
`~/.claude/projects/` until the next `rl triage`, where you re-read enough of it to decide
what it was and whether anything in it was worth keeping.

That re-reading is waste. At the moment you quit you already know what the session was,
whether the work finished, and what in it is worth keeping — better than any later reader,
including a model given the transcript, because you know things that were never written
down. A skill that runs at that moment can capture it for free.

## Constraint: the transcript cannot be archived while the session lives

`rl session complete` harvests and then archives, and archiving moves the transcript out of
`~/.claude/projects/`. That move is unsafe on a live session.

Measured on Claude Code 2.1.245: `lsof -p $CLAUDE_PID` reports **zero** open `.jsonl`
handles. The agent opens, appends and closes on every turn. So moving the file does not
redirect subsequent writes into the archive — the next turn **recreates the transcript at
the original path**. The result is a truncated archive plus a live stub whose session id is
already recorded in the registry's `archived` list, which `rl session refresh` then
rediscovers. The session you just finished walks back into tomorrow's triage.

The two halves of `complete` therefore want opposite timing. Harvesting needs the session's
context and must happen now. Archiving needs the file to be quiet and can only happen once
the process is gone.

## Agent survey

Whether an agent can tell us it is exiting, verified on this machine rather than assumed:

| agent | session-end signal | mechanism |
|---|---|---|
| Claude Code | `SessionEnd` | shell command in `settings.json` |
| Codex | `SessionEnd` | shell command in `hooks.json`, near-identical schema |
| Pi | `session_shutdown`, `reason: "quit"` | TypeScript extension in `~/.pi/agent/extensions/` |
| OpenCode | none | plugin events are `session.created/updated/deleted/idle/error/compacted`; `idle` fires after every turn, not at exit |

Three of four can signal exit, in two different languages. One cannot. No design that
depends on the hook can work everywhere, so the hook cannot be the mechanism.

## Design

The mark is the mechanism; the hook is an accelerator.

```
in-session:   harvest (needs context)  ──►  mark finishOnExit
at exit:      hook fires  ──►  rl session sweep --session <id>  ──►  archive
no hook, or
hard kill:    next refresh/roundup  ──►  rl session sweep  ──►  archive
```

Every path converges on the same end state, and the sweep is idempotent. The hook only
buys latency: seconds after you quit, instead of at your next roundup. That is also what
makes the hook safe to depend on — a `SIGKILL`, a closed terminal, or a hook that fails to
fire cannot strand a session, because the mark survives and the next sweep finds it.

Leaving the triage inbox needs no new machinery: `overview.py:246` already skips threads
whose `decision` is `harvested` or `keep`, and the harvest sets `decision = "harvested"`.
The `finishOnExit` mark exists **only** to tell the sweep that this thread is waiting to be
archived.

### Knowing which session you are

Per-agent knowledge that belongs in the tool, not in skill prose — the same reasoning that
put transcript formats behind the `Source` seam.

A new `ringleader/session_env.py` resolves the current session from the process
environment. Claude Code exports `CLAUDE_CODE_SESSION_ID`; Codex, OpenCode and Pi each get
their own probe, and the module returns `None` when it cannot tell. It is exposed as a
`--current` flag on the commands below, and as `rl session whoami` for debugging and for
the smoke checklist.

The skill therefore never has to know which agent is running it.

### CLI additions

- **`rl session harvest --current --from-file <path|->`** — store agent-authored harvest
  markdown instead of spending a model call on a transcript the agent already has in
  context. Reuses `harvests.write_harvest` and `registry.mark_harvested` unchanged. The
  existing model-backed path is untouched; `--from-file` simply skips the model, so it also
  works with no `rl config` model set.
- **`rl session finish --current`** — set `finishOnExit: true` and `finishedAt` on the
  thread. Refuses if the thread has not been harvested, so `finish` cannot silently discard
  a session's knowledge; `--force` overrides for the deliberate "nothing here worth
  keeping" case.
- **`rl session sweep [--session <id>]`** — archive marked threads whose transcript has
  gone quiet, by calling the existing `archive_thread`. Two forms:
  - bare: every thread with `finishOnExit` whose transcript mtime is older than a grace
    period (default 120s). Called from `refresh`/`roundup`.
  - `--session <id>`: the hook form, which ignores the grace period because the agent has
    told us the session is over. It samples the transcript's mtime every 250ms and archives
    once two consecutive samples match, giving up and archiving anyway after 3s. The bound
    keeps it inside every agent's hook timeout — Codex hooks in this user's `hooks.json`
    are registered with `timeout: 5`.

### The stub guard

Independent of this feature, and a prerequisite for it. Because agents recreate a
transcript they are still writing to, a file can appear at a path whose session id is
already in `archived`. Two changes harden this:

- `rl session refresh` ignores discovered files whose session id is in `archived`, so a
  stub is never re-registered as a live thread.
- `rl prune` deletes such stubs, so they do not accumulate on disk.

This is the failure mode that makes naive "archive yourself" designs wrong, so it is
specified rather than left to the implementation.

### Hooks

The hook artifacts themselves:

- **Claude Code and Codex** share one shell script, registered into `settings.json` and
  `hooks.json` respectively, that pipes the hook's JSON payload on stdin to
  `rl session sweep --from-hook`. Claude Code puts the session id in a top-level
  `session_id` field, verified on 2.1.245; Codex's schema is near-identical and
  *assumed* to match, unverified against a real payload. The schemas otherwise differ
  only in file location and casing.
- **Pi** gets a small TypeScript extension in `~/.pi/agent/extensions/`, subscribing to
  `session_shutdown` and acting when `reason === "quit"`.
- **OpenCode** gets nothing and falls back to the sweep. `rl doctor` says so explicitly
  rather than reporting a failure, since there is nothing to install.

Writing an agent's config file is a mutation of the user's environment, so installing
hooks confirms before writing and is never run implicitly by another command.

### Where the install commands live

Hooks are the **third** thing ringleader installs into an agent's configuration, after
skills and the statusline. Both existing ones are noun groups with the same four verbs:

```
rl skills      install | update | status | uninstall
rl statusline  install |          status | uninstall
rl hooks       install | update | status | uninstall   (this design)
```

Adding a third copy of the same verb set is the moment to invert it. The surface becomes
verb-first, with the noun as an argument:

```
rl install    [skills|hooks|statusline|all]   # bare `rl install` = all
rl update     <skills|hooks|statusline|all>   # noun required
rl uninstall  <skills|hooks|statusline|all>
rl doctor                                     # absorbs all three `status` commands
```

This is a better fit for the rule the CLI already follows — flat verbs are what a human
types, groups are plumbing the skills call — because installing is exactly the human-facing
setup step. `_GROUPS` drops from five to three (`session`, `mem`, `data`), and the one
command a new user needs is `rl install`.

Three details the implementation must preserve:

- **`install` and `update` stay distinct.** `install` opts an agent in and confirms;
  `update` only re-syncs what is already wired and is therefore safe unattended. This is
  what lets `just install-local` end in an update without silently wiring ringleader into
  every agent on the machine.
- **Bare `rl update` is an error, not "everything".** Unlike `install`, a bare `update`
  reads as "update ringleader itself"; requiring the noun removes the ambiguity.
- **`update statusline` is not an error either.** The statusline has no update semantics,
  so `rl update all` skips it and says so rather than failing.

Absorbing `status` into `rl doctor` expands doctor from its current single retention check
into a full setup report: retention, plus what is linked or registered for each detected
agent. That is what a user already expects `doctor` to answer.

Migration touches `ringleader/cli.py` (`_FLAT`/`_GROUPS`), `ringleader/commands/skills.py`
and `statusline.py` (argparse `prog=` strings and the dispatch), `Justfile` (three recipes
call `rl skills update --yes`), `README.md`, `AGENTS.md`, `.agents/skills/SMOKE.md`, and
`.agents/skills/ringleader-init/SKILL.md`, which offers to wire skills up during onboarding.

### The skill

`ringleader-finish`, agent-neutral in `.agents/skills/`, with the bundled
`references/playbook.md` synced from `PLAYBOOK.md` by `just sync-playbook` like the other
five.

1. Resolve the session (`rl session whoami`) and state what finishing will do.
2. Confirm the work is actually finished. If it is not, the single escape hatch is
   `rl session snooze` — anything else is left to normal triage.
3. Write the harvest from live context and pipe it to
   `rl session harvest --current --from-file -`, following the harvest sub-protocol's
   content conventions.
4. `rl session finish --current`.
5. Report when the archive will happen: at exit on Claude/Codex/Pi with hooks installed,
   at the next roundup otherwise.

## Data model

One new field on a thread, alongside the existing `keptAt` / `snoozedUntil` dispositions:

```
finishOnExit: bool     # waiting to be archived once the transcript goes quiet
finishedAt:   iso8601  # when the skill marked it
```

`archive_thread` clears both as part of moving the record into `archived`. No registry
schema version bump: this is an additive optional field, and older records simply lack it.

## Delivery order

Three phases, each independently useful and independently reviewable:

0. **The verb-first install surface.** Move `skills` and `statusline` under
   `rl install`/`update`/`uninstall`, fold their `status` into `rl doctor`. This touches
   none of the finish feature and is worth landing on its own merits; it is phase 0 only
   because hooks would otherwise add a fourth copy of the old shape. It could reasonably be
   split into its own spec.
1. **Mark, sweep and skill.** `session_env`, the three `rl session` additions, the stub
   guard, and `ringleader-finish`. This is the whole feature on every agent, archiving at
   the next roundup. Nothing here touches an agent's config.
2. **Hooks.** The two hook artifacts plus `rl install hooks`, turning "next roundup" into
   "at exit" on Claude, Codex and Pi.

Phase 1 must precede phase 2, which only calls a sweep that phase 1 defines. Phase 0 is
independent of both, but doing it first avoids migrating a surface twice.

## Testing

- `session_env` resolution per agent, with monkeypatched environments, including the
  unknown-agent case returning `None`.
- The sweep's predicates: marked-and-quiet archives, marked-but-fresh does not,
  unmarked-and-quiet does not.
- `harvest --from-file` round-trip through the harvest store, asserting the stored content
  and that `decision` becomes `harvested`.
- `finish` refusing an unharvested thread, and `--force` overriding it.
- The stub guard: a file whose id is in `archived` is not re-registered by `refresh` and is
  removed by `prune`.
- The command migration: `rl install`/`update`/`uninstall` dispatch to the right noun,
  bare `rl install` covers all three, bare `rl update` errors, `rl update all` skips the
  statusline with a note, and `rl doctor` reports skills, hooks and statusline state.
  `tests/test_skills_cmd.py` moves with the surface.
- Hook scripts get an entry in `.agents/skills/SMOKE.md` rather than automated tests — they
  need real agent processes to exercise.

## Out of scope

- OpenCode exit detection. Its plugin API has no exit event; `session.idle` is the wrong
  semantics and polling for process death is not worth the machinery when the sweep already
  covers it.
- Changing `rl session complete`. It stays exactly as it is for the triage path, where the
  session is already dormant and both halves can run together.
- Automatic finishing. The skill is invoked deliberately; nothing infers that a session is
  done.

## Risks

- **A hook that fires too early.** If an agent runs `SessionEnd` before its final write,
  the mtime poll absorbs it; if a write lands after the poll gives up, the stub guard
  catches it. Both paths are specified above, but the interaction is the most likely source
  of bugs.
- **A breaking change to a documented surface.** `rl skills install` and
  `rl statusline install` are in the README, in `SMOKE.md`, in the `ringleader-init` skill
  and in three `Justfile` recipes. The move is mechanical but wide, and anything missed
  fails only when a user or skill runs it. This is the main argument for landing phase 0 by
  itself.
- **Three config formats to keep working.** Agents change their hook schemas. `rl hooks
  status` should therefore report what it finds rather than assuming what it wrote is still
  valid.
- **`ringleader/commands/harvest.py` is modified in the working tree by another session.**
  This design touches that file. It is being written on the `finish-session` worktree
  branched from `3de2422` to keep the two apart, but the change has to be reconciled before
  it lands.
