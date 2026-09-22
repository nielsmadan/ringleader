# Session portability internals

`portability/` is independent of cheap `sources/` discovery and metadata parsing.
`adapters.py` binds deep readers, native writers, launchers, roots, optional identity
allocation, native name-index operations, and DB registration. Command handlers
resolve capabilities; they do not branch on native transcript formats.

## Preservation and snapshots

`model.py` and `serialization.py` define versioned conversations with separate
visible history and active model context, provenance-bearing instructions and
historical timestamps, call/result pairs, owned attachments, and checkpoints.
`Checkpoint.following_context` preserves an independently identified post-boundary
model suffix. `compare.py` checks both streams, their order/provenance/content,
checkpoint semantics and attachment digests; message IDs may be regenerated.
Claude/Pi's equivalent-stream gate ignores message IDs/parents but retains all
other fields, so genuinely different native visibility/provenance still fails.

`compact.py` uses no model. It keeps the checkpoint and retained context contract,
retains instructions/prose, and replaces complete contiguous tool exchanges with
verified omission markers. Historical metadata may outlive an omitted tool; for
example OpenCode tool-time provenance remains historical when an explicit omission
marker replaces the native tool parts.

`snapshot.py` bounds and verifies file identity/content and attachment reads with
retries. OpenCode snapshots stable DB/WAL copies then reads the graph in one SQLite
transaction. Neither preview nor schema inspection creates native-store sidecars.
An adapter cannot silently truncate an unfinished source turn.

## Service and persistence

`ConversionService.convert()` snapshots, resolves target profile/configuration,
transforms, stages, reads back, compares, and checks picker identity/title/cwd
before recording an operation. `operations.py` persists versioned, digest-checked
artifacts and intents under `conversion/`; full execution roots are stored for
recovery. Semantic reuse identity includes target store/cwd/profile/schema/resolved
configuration and source snapshot/options/title, but not incidental TMP/cache paths.

The conversion operation lock precedes the short registry mutation lock. Slow
snapshot/staging work happens outside it. Registry updates reload current state and
merge bounded mutations. A concurrent scan, inbox edit, or unrelated metadata
update must not overwrite conversion lineage or another writer's fields.

New files use create-only publication with durable ownership evidence; a target
collision is never overwritten. Codex appends its name-index intent before rollout
visibility and then inserts the thread into its schema-gated DB. OpenCode inserts
its new project/session/message/part graph in one transaction. Shared databases
are neither replaced nor migrated. Native creation and recency use current time;
message times stay historical and imported usage is zero.

Each external boundary has an intent/result. A native session may be discoverable
before registry publication completes. Recovery is therefore forward-only after
possible visibility, and acknowledges owned records while preserving subsequent
native modifications. Uncertain ownership or a missing target after an ambiguous
intent fails with inspection guidance. Rollback only removes proven private
staging; private publisher generations and unverified residues are journaled.

Registry `conversionLineage` is authoritative for source/target relationships;
thread `lineage` and `nativeRoots` support discovery/launch and survive lifecycle
transitions and scans. Repeated conversion reports archived/deleted results rather
than recreating them. Fork bypasses reuse with a fresh operation identity.

`selection.py` resolves registry identities/visibility and verified presence.
`commands/conversion.py` constructs requests, per-source batch results, and launch
failure diagnostics; `commands/transactions.py` exposes recovery. `launch.py`
validates executable/cwd and uses `execvpe`, retaining the terminal and normal user
environment. Native roots/cwd overrides are explicit and persisted.

See [format and native verification reference](../reference/session-portability-native.md)
for schema gates, annotation validation, unsupported semantics and offline smoke
commands. `tests/portability/test_matrix.py` separates generated shared-content
roundtrips from independently native-shaped sources and their failure contracts.
