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

## Session analysis

`rl analyze` is implemented (see [analysis](features/analysis.md)). These directions were left out
of it on purpose.

- **An inbox analysis screen.** The inbox does not read the analysis index. A screen would show
  the worst sessions and recurring issues beside triage, with one key to open a session's detail.
- **Local-model label defaults.** Labels use a cheap hosted model through the agent CLI engines.
  A local model default would make `--label` free, but needs the labelling benchmark
  ([tests/analyze](tests/analyze/README.md)) run against it first. The benchmark has not been run on
  a real model yet.
- **Label garbage collection.** Labels for episodes that no longer exist, and for superseded
  prompt versions, are never removed. A cleanup would need to decide which labels are still wanted.
- **Parallel extraction.** A cold build reads every transcript in one process and took 205 s on
  about 11,600 transcripts. Extraction is per transcript and independent, so it could fan out.
- **A faster JSON parser.** `orjson` was not added because the cold build stayed under the 5
  minute bar. Revisit if cold builds grow past it.
- **Append-only re-extraction.** A changed transcript is re-read whole. A live session that only
  appended could resume from its last line.
- **Merge before the session filter.** Recurring issues drop a signature seen in too few sessions
  before near-duplicates are merged, so near-duplicates that each appear once never surface. Merging
  first would surface more issues when the `embed` extra is installed.
- **The deferred precision notes.** Existence probes, failed writes counted as writes, runaway
  windows a human message does not reset, and Droid's `Create` tool. Each is a small detector change
  plus a `DERIVE_VERSION` bump and a re-calibration. See
  [Deferred precision notes](tech/analysis.md#deferred-precision-notes).
- **Analysis for Droid, OpenCode and Pi.** They contribute tokens only. Calls, signals and episodes
  need an extractor per agent.
