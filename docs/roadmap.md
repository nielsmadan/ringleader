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
publication are implemented under `rl session` for pinned profiles. The top-level
`rl resume --to` still uses a seeded Markdown handoff, as described in
[ADR 0011](decisions/0011-seeded-handoff-over-native-conversion.md).
Future work includes additional
native versions/schemas, more ordinary conversational shapes, broader OpenCode
configuration, and independent OpenCode runtime validation. The current strict
limitations and evidence tiers are in the
[native reference](reference/session-portability-native.md).

## Transactional native relocation

Current workspace portability moves a checkout, relocates supported session storage, and pins registry locations, with `retarget` available after a partial failure.
It refuses destination merges and does not provide a general transaction journal.

The broader design remains unimplemented: per-session native history moves for every supported agent, structured-field-only rewrites, staged publication with checksummed rollback, crash recovery, cross-filesystem manifest verification, and Hooklinesinker liveness gating.
Any future work must preserve the current rule that historical prompts, commands, tool arguments, and tool results are never search-and-replaced.
