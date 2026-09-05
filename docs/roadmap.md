# Unimplemented designs

This file records approved or explored directions that are not part of Ringleader's current behavior.
They must be revalidated against the current source and external agent formats before implementation.

## Shared live-session presence

Hooklinesinker and Ringleader's presence client are implemented. `rl data presence`
provides installation, status, and scoped uninstallation; `rl doctor` reports presence health.
The client reads Hooklinesinker's versioned CLI JSON and keeps its state files private.

A verified `--running` session selector remains unimplemented. It will use the presence
client's agent-qualified running-session IDs alongside Ringleader's transcript discovery.

## Cross-agent session portability

The proposed portability layer converts native agent sessions through one versioned canonical conversation model rather than pairwise converters.
Independent adapter capabilities would cover conversation reading, native writing, launching, and relocation for Claude Code, Codex, OpenCode, and Pi.

The design includes out-of-place conversion, same- or cross-agent forks, deterministic compact conversion, resume and handoff commands, lineage, staged round-trip validation, and per-session transactional publication.
It excludes hidden model reasoning, cache state, token accounting, and re-execution of historical tool calls.

None of the canonical model, four adapters, conversion, fork, resume, handoff, or lineage commands is implemented.

## Transactional native relocation

Current workspace portability moves a checkout, relocates supported session storage, and pins registry locations, with `retarget` available after a partial failure.
It refuses destination merges and does not provide a general transaction journal.

The broader design remains unimplemented: per-session native history moves for every supported agent, structured-field-only rewrites, staged publication with checksummed rollback, crash recovery, cross-filesystem manifest verification, and Hooklinesinker liveness gating.
Any future work must preserve the current rule that historical prompts, commands, tool arguments, and tool results are never search-and-replaced.
