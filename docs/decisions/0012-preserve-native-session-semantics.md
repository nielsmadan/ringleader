# 0012: Preserve native session semantics during conversion

## Context

Agent sessions contain more than visible prose: tool exchanges, attachments, authored
instructions and compaction boundaries determine what a resumed model receives.
Visible history and active model context can differ. Pairwise converters multiply
format-specific logic, while replaying a transcript as a prompt loses those distinctions.

Publication also crosses native files, indexes, databases and Ringleader's registry.
Another process can resume a target before every boundary has completed, so a failed
conversion cannot safely restore an earlier snapshot of the native store.

## Decision

Use one versioned canonical conversation with separate visible and model histories,
provenance, matched tool exchanges, owned attachments and checkpoint semantics.
Keep deep readers, native writers and launchers behind independent capabilities,
separate from cheap scanning through `Source`.

Preserve supported semantics or refuse before publication. Historical instructions
remain history; conversion does not promote them to current system policy. Historical
tools remain inert. Exclude harness bootstrap only when its structure establishes
its role, never because its text resembles a known prompt. Compact conversion is a
deterministic, explicit omission of supported tool exchanges, without model-generated
summaries. Preserve message timestamps even where a native picker sorts old imports
below recent conversations.

Gate adapters on known native contracts. Read staged output back, compare both
conversation streams, and independently validate native identity, title and cwd.
Roundtrips establish internal consistency; native runtime checks establish a different
level of evidence, recorded separately in the
[native reference](../reference/session-portability-native.md).

Publish create-only files and narrowly scoped native database/index changes with
durable intents and ownership evidence. Recover forward after possible native
visibility, preserving later native changes. Never replace a shared database or
recreate a target whose ownership is ambiguous. Bind recovery to its original registry.

Protect lineage across all overlapping registry writers. Merge bounded changes from
a caller's baseline into freshly locked state; a conversion-only lock cannot protect
against stale scan or inbox writes. Keep slow work outside the registry lock and make
same-field conflicts explicit.

## Consequences

New native shapes may require model or adapter changes before they can be converted.
Evidence differs by agent; supported profiles do not imply every native behavior has
been exercised. Ambiguous recovery can require manual inspection and leave unverified
private artifacts in place.

Converted sessions follow ordinary lifecycle policy. Idempotent conversion reports
an archived or deleted result instead of recreating it; a fork requests a fresh identity.
Workspace relocation remains a separate operation.

Current contracts live in [session portability internals](../tech/session-portability.md)
and [registry concurrency](../tech/registry.md#concurrent-mutations).
