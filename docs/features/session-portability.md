# Session portability

`session convert`, `fork`, `handoff`, `resume`, and `lineage` provide native session
continuation across Claude Code, Codex, OpenCode, and Pi. Conversion preserves the
source and publishes a fresh target in the source's effective recorded cwd.
Workspace move/retarget is a separate feature.

- Source identity is `(agent, native ID)`; explicit IDs/prefixes can be qualified.
  Selection reads the registry without refresh. `--open` follows effective open
  lifecycle and default visibility; `--running` requires complete verified presence
  and ignores lifecycle/visibility. `--from` and `--workspace` filter sources.
- Snapshots include branch selection, attachment bytes, provenance, visible history,
  active context, and compaction. Unknown required semantics and incomplete sources
  fail. Excluded data is hidden reasoning, usage accounting, caches, and positively
  identified harness bootstrap. Historical tools are inert; no call is replayed.
- `--compact` deterministically honors the latest checkpoint/retained tail, keeps
  authored instructions and remaining prose, and replaces complete tool exchanges
  with explicit names/counts. Reports are approximate text sizes, not billing data.
- `convert` is idempotent for matching snapshot/options/profile/store/cwd/config.
  `fork` always allocates a new target; `handoff` forks then launches. Native resume
  uses adapter argv plus recorded cwd without a shell or automatic prompt.
- Dry-run writes no persistent state. Per-session publication journals make batches
  independently recoverable; partial failure retains successes and exits nonzero.
  Native file/index/DB boundaries are not one atomic visibility point.
- Once externally discoverable, recovery is forward-only. Rollback removes only
  verified private staging. Native edits, renames, unrelated rows and lineage survive
  retries. Archived/deleted results are reported rather than silently recreated.
- Current creation metadata and zero synthetic usage accompany historical message
  times. Pi's native picker still sorts by historical message times. Converted
  sessions start unclassified and are not automatically kept or pruning-exempt.

The full/compact 4×4 matrix covers shared supported semantics, including successful
image results and textual errors. Independent ordinary-source fixtures separately
exercise strict refusals, particularly distinct Codex model/display histories,
unsupported compaction forms, and failed-result images in Claude. Native evidence
has different strengths per agent; OpenCode runtime remains unverified.

See [user workflow](../user/session-portability.md), [implementation](../tech/session-portability.md),
and [exact native contracts](../reference/session-portability-native.md).
