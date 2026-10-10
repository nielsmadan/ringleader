# Session portability internals

`portability/` is independent of cheap `sources/` discovery and metadata parsing.
`adapters.py` binds deep readers, native writers, launchers, roots, optional identity
allocation, native name-index operations, and DB registration, for Claude Code,
Codex, OpenCode, Pi, and Droid. Command handlers resolve capabilities; they do not
branch on native transcript formats.
See [the preservation decision](../decisions/0012-preserve-native-session-semantics.md)
and [its native-conversion amendments](../decisions/0013-native-conversion-behind-resume.md)
for the model, publication, concurrency, and self-verification tradeoffs.

## Self-verification

`verification.py` accepts a target version against each adapter's small upstream-pinned
list (`PINNED` in `claude.py`/`codex.py`/`pi.py`; empty for `droid.py`, which pins
nothing) or a version recorded in a local `verified-targets.json` after an offline
harness pass. Harnesses live in `portability/verify/` — a loopback-HTTP model-request
capture for Claude Code/Codex/Pi/Droid (tier `model-request-capture`), a CLI-acceptance
and schema check for OpenCode (tier `cli-acceptance`) — and run via
`rl doctor --verify-conversion [AGENT]` or automatically, interactively only, from
`native_resume.native_resume()` the first time `rl resume --to` needs an unverified
target. A per-agent `HARNESS_VERSION` invalidates stored records when the harness logic
itself changes, and each record carries a digest of every `portability/*.py` file, so a
changed check or changed writer code always re-verifies rather than trusting a stale pass.

The layering runs one way. `verification.py` is a leaf: the record type, the state file,
`HARNESS_VERSION`/`TIERS` and the code digest, which the adapters import to gate versions.
Each `verify/<agent>.py` harness imports its adapter (and those records), and
`adapters.py` registers it as that adapter's `Verifier` (executable plus harness), so
nothing looks a harness up by agent name. `verify/workflow.py` holds the single
`verify_and_record()` both entry points call: it resolves the `Verifier` through the
registry, runs the harness in a scratch directory, keeps failure evidence (the newest
three runs per agent, links copied as links) in `verify-failures/` beside the state file,
records the pass, and reports every line through a callback. Doctor prints those lines on
stdout and `rl resume` on stderr, so the two never word a result differently.

## Preservation and snapshots

`model.py` and `serialization.py` define versioned conversations with separate
visible history and active model context, provenance-bearing instructions and
historical timestamps, call/result pairs, owned attachments, and checkpoints.
`Checkpoint.following_context` preserves an independently identified post-boundary
model suffix. `compare.py` checks both streams, their order/provenance/content,
checkpoint semantics and attachment digests; message IDs may be regenerated.
Claude/Pi's equivalent-stream gate ignores message IDs/parents but retains all
other fields, so genuinely different native visibility/provenance still fails.
`compare.py` is also a write-side library: `CheckpointShape`, `flattened()`,
`model_view()` and `joined_tool_output()` are imported by every writer to flatten a
checkpoint it cannot express (R29), and the gate builds its expected view with the same
`flattened()`. The gate therefore cannot catch a defect in those transforms; the
per-agent harness is what exercises them against the real CLI.

`compact.py` holds two transforms and uses no model in either. `portable()` runs on
every conversion: it swaps `Opaque` content the target agent cannot replay for its
fallback, collapses a checkpoint's opaque summary into its replaced context (deduping
Codex's retained copies of user messages against it), and trims the oldest collapsed
history behind one omission marker when the result exceeds `MAX_PORTABLE_TOKENS`.
`compact()` runs only for `--compact`: it keeps the checkpoint and retained context
contract, retains instructions/prose, and replaces complete contiguous tool exchanges with
verified omission markers. Historical metadata may outlive an omitted tool; for
example OpenCode tool-time provenance remains historical when an explicit omission
marker replaces the native tool parts.

`snapshot.py` bounds and verifies file identity/content and attachment reads with
retries. OpenCode snapshots stable DB/WAL copies then reads the graph in one SQLite
transaction. Neither preview nor schema inspection creates native-store sidecars.
An adapter cannot silently truncate an unfinished source turn. Claude selection
retains the terminal record on the selected branch, so trailing unsupported records
are validated even when the last visible message precedes them. OpenCode attachment
discovery visits native file parts only; file-shaped tool arguments remain inert data.

## Service and persistence

`ConversionService.convert()` snapshots, resolves target profile/configuration,
transforms, stages, reads back, compares, and checks picker identity/title/cwd
before recording an operation. `operations.py` persists versioned, digest-checked
artifacts and intents under `conversion/`; full execution roots are stored for
recovery. Semantic reuse identity includes target store/cwd/profile/schema/resolved
configuration and source snapshot/options/title, but not incidental TMP/cache paths.
The requested reuse key is computed once before taking the operation lock.
`prepare()` reads the staged output back and probes the target once; `publish()` of
that prepared conversion reuses the read-back and replays the recorded version probe,
re-reading only the target's configuration and schema to catch drift since `prepare()`.
Journals recovered from disk get the full checks again. `index.json` in the operations
directory maps each operation to its reuse key and source/target identities, so reuse
and recovery lookups decode only matching journals. It is rewritten under the operation
lock on every create and reconciled against the directory listing on each read, so a
missing, corrupt or stale index costs a rescan, never a wrong answer.

Journal/artifact version 2 binds each operation to its resolved registry path.
Resume, rollback and reuse reject a different registry before changing state, with
the original path in the diagnostic. Legacy unbound version 1 journals fail closed;
they cannot safely infer which registry originally owned the operation.

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

File publication prepares a hidden sibling beside the target, so the journal and
native store may reside on different filesystems. The complete file is linked into
place without overwriting an existing target. If private preparation cannot establish
ownership, recovery can retain the residue and journal a new publisher generation,
but only before that file's final publication intent. An earlier index append may
already prevent rollback; it does not prevent this private preparation retry.
Once the final-file intent exists, a missing or replaced target requires inspection
rather than another publication attempt. Cleanup never guesses ownership from a path.

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
