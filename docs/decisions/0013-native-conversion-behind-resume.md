# 0013: Put native conversion behind `rl resume --to`, amend ADR 0012

## Context

[ADR 0011](0011-seeded-handoff-over-native-conversion.md) shipped the picker and a seeded
Markdown handoff for cross-agent continuation, deferring the portability layer.
[ADR 0012](0012-preserve-native-session-semantics.md) then built that layer: one canonical
conversation model, deep readers/writers, a compact transform, staged publication, and a
transaction journal, exposed as `rl session convert/fork/handoff/resume`. `rl resume --to`
itself still only did the seeded handoff; reaching a real native continuation meant separately
learning `rl session convert`/`fork`.

Converting real captured sessions for five agents — not just synthetic fixtures — also surfaced
native shapes ADR 0012 did not anticipate: checkpoints a target cannot express natively, content
only one agent can interpret, and agent releases newer than whatever version the adapters were
last built against.

## Decision

**`rl resume --to` tries native conversion first.** It reuses or reopens an already-converted,
still-active target; forks a fresh one when the previously converted target was archived,
deleted, or otherwise gone. On refusal it asks before falling back to the ADR 0011 seeded
handoff; `--dry-run` reports what would happen without writing anything; `--markdown` skips the
native attempt and goes straight to the seeded handoff. ADR 0011's mechanism is retained as that
fallback, not replaced.

**Self-verification of target versions.** Each adapter accepts an installed agent version when
it matches either a short list pinned from upstream source inspection, or a version
self-verified on this machine and recorded in a local state file (`verified-targets.json`,
`--verified PATH` to isolate it). Self-verification runs an offline harness per agent: a
loopback-HTTP capture of the actual outbound model request for Claude Code, Codex, Pi, and Droid
(tier `model-request-capture`), and a CLI-acceptance/schema check for OpenCode (tier
`cli-acceptance` — its real outbound request needs a source checkout to inspect, which the
harness does not require). Droid ships with no pinned versions at all; it is entirely
self-verify-driven. A harness-logic change bumps that agent's harness version, and a stored
record under an older harness version is ignored, so a changed harness re-verifies rather than
trusting a stale pass. `rl resume --to` runs this self-verification itself, interactively only,
the first time it needs an unverified target; it never verifies silently or non-interactively.
`rl doctor --verify-conversion [AGENT]` is the standalone entry point.

**Two-streams model-view rule, extended to checkpoint flattening.** When a target cannot express
a checkpoint in its own native shape — a foreign summary wrapper, hidden messages after the
checkpoint, independent retained/following context — the writer flattens it to an ordinary
stream (summary messages, then retained context, then later turns, with hidden messages written
as that target's own hidden form or as ordinary messages) instead of refusing. This is recorded
as a declared normalization, not silent loss, and applies to every writer. This amends ADR
0012's "preserve supported semantics or refuse before publication": flattening is now a
supported semantic in its own right, not something only a refusal protects against.

**Opaque content and budget-driven `portable()` substitution.** Content only one agent can
interpret — Codex's encrypted compaction payload, for instance — is modeled as an opaque value
carrying both its native payload and a fallback rendering: carried verbatim to that same agent,
substituted by the fallback for every other target. When the substituted content would push a
converted context over a target's budget, the oldest collapsed history is trimmed first, whole
tool-exchange/turn units at a time, behind one explicit harness-provenance marker naming the
count — not a refusal. Conversion refuses only when the checkpoint's own retained context plus
later turns alone already exceed the budget. This amends ADR 0012's "deterministic, explicit
omission": that principle already covered the `--compact` transform; it now also covers this
budget-driven substitution outside `--compact`.

**Harness bootstrap is dropped structurally, confirmed across all five agents.** A system or
developer prompt an agent's own harness injects is identified by where it lives in the native
structure — a dedicated field, a specific record type — never by matching its text against a
known prompt. Structurally-identified bootstrap is excluded for every target, including the
agent the session came from, so a converted session gets that target's own current bootstrap
rather than an imported one. This confirms ADR 0012's existing rule against real captured
sessions from Claude Code, Codex, Pi, OpenCode, and Droid.

**Claude attachment classification confirmed against native resume.** Each Claude Code
attachment kind is classified once, by kind — carried with its original rendering, carried via
a reconstructed template, or refused — by reading the installed CLI's own bundle. That
classification was re-confirmed by comparing a converted session's outbound request against an
actual non-bare `claude --resume` of the same content: the two send identical attachment text.
The classification is per-kind and fixed, not inferred live from the SDK's bare-mode history
read, which itself drops attachments that bare mode never resumes.

## Consequences

- `rl resume --to` and `rl session convert/fork/handoff` are now the same mechanism: there is one
  answer to "what happens when I resume in a different agent," not two.
- A newer agent release does not break conversion outright; self-verification absorbs it, at the
  cost of a one-time harness run instead of a doc or pin update.
- A target's own compaction UI can show a flattened checkpoint as a plain message instead of its
  native summary widget; the model's active context is unchanged either way.
- A Codex-only payload substituted into another agent's context, or a budget-trimmed oldest
  history, is explicit and recoverable from its omission marker, but it is still lossy — a user
  relying on that content for continuation has to go back to the source session.
- Harness bootstrap never survives a conversion, by construction: a custom system prompt an
  agent's harness injects is not something `rl` carries, even into that same agent's own
  converted session.
- Evidence strength still differs by agent — OpenCode remains CLI-acceptance only, having no
  verified outbound-request capture. Exact tiers and the current pairwise conversion rates are in
  [the native reference](../reference/session-portability-native.md).

## Alternatives considered

**Keep every target pinned to the one exact version it was built against.** Simpler to reason
about, but every agent release then breaks conversion until ringleader re-pins — the thing ADR
0011 already chose not to block the picker on. Self-verification trades a one-time local check
for staying current without a code change.

**Refuse rather than flatten, substitute, or trim.** The more conservative reading of ADR 0012's
"preserve or refuse." Rejected because it left ordinary real sessions — not synthetic edge
cases — failing to convert at a rate well above what a working tool should accept, with no
better alternative than the lossier Markdown handoff anyway. Explicit, bounded, recorded loss is
the better trade.
