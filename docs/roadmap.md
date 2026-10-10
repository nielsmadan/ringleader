# Unimplemented designs

This file records approved or explored directions that are not part of Ringleader's current behavior.
They must be revalidated against the current source and external agent formats before implementation.

## Shared live-session presence

Hooklinesinker and Ringleader's presence client are implemented. `rl data presence`
provides installation, status, and scoped uninstallation; `rl doctor` reports presence health.
The client reads Hooklinesinker's versioned CLI JSON and keeps its state files private.

Verified `--running` selection is implemented for session conversion. See
[session portability](features/session-portability.md) for current behavior.

## Broader native session compatibility

Conversion, compact conversion, forks, resume, handoff, lineage, and durable
publication are implemented under `rl session`, for Claude Code, Codex, OpenCode,
Pi, and Droid, gated by self-verification rather than a single pinned version per
agent (see [ADR 0013](decisions/0013-native-conversion-behind-resume.md)). The
top-level `rl resume --to` now uses this same native conversion, falling back to
the seeded Markdown handoff described in
[ADR 0011](decisions/0011-seeded-handoff-over-native-conversion.md) only on
refusal or `--markdown`. Future work includes more ordinary conversational
shapes, broader OpenCode configuration, and independent OpenCode runtime
validation (its evidence tier is CLI-acceptance only, with no verified outbound
model-request capture). The current strict limitations and evidence tiers are in
the [native reference](reference/session-portability-native.md).

Cross-machine bundles, full branch-tree reproduction and model-generated summaries
were outside the conversion scope and remain unimplemented. Recovery journals are
bound to their original registry; moving them to another registry would need an
explicit migration mechanism.

Known gaps left open when conversion was made to work on real sessions:

- **Journal retention.** Conversion journals under `conversion/` are never pruned
  and hold copies of each source; failure evidence keeps the newest three runs per
  agent.
- **Snapshot cap on shared databases.** The 128 MB snapshot limit also applies to the
  whole OpenCode `opencode.db` and Codex `state_5.sqlite`; once either outgrows it,
  every conversion from that agent refuses.
- **Rare native shapes.** Codex `thread_rolled_back` post-cut reordering, Claude
  attachment kinds seen only in the bundle (`skill_mention`, `inlined_image_paths`,
  `account_memory_recall`, `elapsed_time_reminder`, `proactivity`,
  `teammate_mailbox`, `team_context`) and OpenCode sessions with two tool parts
  sharing one call id all refuse until real samples exist.
- **Droid resume.** A converted droid session is verified by `droid exec --fork`
  replay; a direct `droid resume` of one needs a logged-in Factory account and has not
  been exercised.
- **Structure.** The Codex, OpenCode and Claude adapters each hold reader, writer
  and adapter in one large module, `compare.py` is both the preservation judge and a
  write-side transform, the portable-context budget is one global constant, and the
  Codex retained-message matching lives in the agent-neutral `compact.py`. Splitting
  these is a refactor with no behaviour change.

## Transactional native relocation

Current workspace portability moves a checkout, relocates supported session storage, and pins registry locations, with `retarget` available after a partial failure.
It refuses destination merges and does not provide a general transaction journal.

The broader design remains unimplemented: per-session native history moves for every supported agent, structured-field-only rewrites, staged publication with checksummed rollback, crash recovery, cross-filesystem manifest verification, and Hooklinesinker liveness gating.
Any future work must preserve the current rule that historical prompts, commands, tool arguments, and tool results are never search-and-replaced.
