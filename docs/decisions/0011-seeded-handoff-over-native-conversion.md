# 0011: Hand off to another agent by seeding it, not by converting the session

## Context

`rl resume` re-enters a dormant session. Re-entering it in the agent it came from is a solved
problem: every agent exposes a resume verb, so the work is resolving the id and the directory and
then `exec`ing. Continuing it in a *different* agent is not.

The approved design under `docs/specs/2026-08-29-session-portability-design.md` answers that with
a portability layer: one versioned canonical conversation model, a `ConversationReader` and
`ConversationWriter` per agent, a compact transform, staged round-trip validation, and a
transaction journal. The target then genuinely continues the conversation in its own native
store, with lineage recorded on both sides.

That design is sound and remains the right end state. It is also a large project — four adapters,
sanitized native fixtures per agent per format version, and a fault-injected transaction machine —
and none of it exists. Meanwhile the picker that makes resuming pleasant is a day's work, and it
is the part a person uses every day.

## Decision

`rl resume` ships the picker now, and implements "continue in a different agent" as a **seeded
handoff**:

1. Render the source session to Markdown with the existing `export.render_conversation`.
2. Write it under `~/.local/share/ringleader/handoffs/`, owner-only and atomically.
3. Launch the target agent **fresh**, in the source session's directory, with a prompt naming the
   original agent and pointing at that file.

The target starts a new session that has *read* the old one. It does not continue it.

The source session is never modified, archived or marked — the same rule the portability design
states for conversions and forks, for the same reason: a handoff that goes badly must leave the
original exactly where it was.

## Consequences

- Cross-agent continuation works today, for all four agents, with no new adapters and no new
  native format knowledge. The only capability required is `SessionLaunchingSource.launch_argv`.
- It is **lossy, and how lossy depends on the source**. Every handoff carries prose only — no
  tool results, no native turn structure, no compaction boundaries — and the target has to
  re-read the file to know anything, which costs it context on its first turn. A Claude Code
  transcript additionally carries the names of the tools each assistant turn called
  (`export._tool_note`); codex, pi and opencode transcripts reach the target with no trace that
  tools ran at all.
- There is **no lineage**. A handoff cannot record what it produced, because the target's session
  id does not exist until the agent creates it. The portability design's lineage fields therefore
  have nothing to migrate from when they land.
- Nothing prunes `handoffs/`. Unlike `archive/`, which `prune` and `roundup` purge on a retention
  clock, handoff transcripts accumulate. They are full conversations, so they are written
  owner-only, but retention is unbuilt and deliberately out of scope here.
- When the portability layer arrives it **replaces** this mechanism rather than extending it.
  `rl resume --to` keeps its spelling; what happens behind it changes from "seed a new session"
  to "convert and resume". The seam that makes that swap cheap is `launch_argv` living on the
  `Source` protocol rather than in the command.

## Alternatives considered

**Build the portability layer first.** The honest end state, and the reason this ADR exists
rather than a quiet shortcut. Rejected for sequencing, not merit: it would have deferred the
picker — the part with daily value — behind a multi-week project, and the recovered execution
ledger for that project already records the instruction to keep it separate from the resume
workflow.

**Ship same-agent resume only.** Smaller and fully honest. Rejected because the agents already
each have their own resume picker; a cross-agent picker whose sessions can only ever return to
the agent they came from adds little over what `claude --resume` and `codex resume` already do.

**Summarise the transcript with a model before handing it over.** Better seeding, but it makes
`rl resume` depend on an LLM and a network call on a path whose whole appeal is that it is
instant. The portability design already scopes a deterministic `--compact` transform and a
separate `--summarize`; neither belongs in this command.
