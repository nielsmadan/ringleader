# Native memory review workflow

## Scope and review

Start with `rl mem triage --json`. Add `--project SELECTOR` and/or
`--source claude-code|codex` only for the requested scope. Use the same `--registry PATH`
throughout a custom-registry run, including harvest assignment and the publisher.
Use `uv run rl` from an uninstalled source checkout. A normal scan can update the
catalog. Use `--dry-run` on triage for a scan without persisting it, or `--no-scan`
for cached metadata. A cold dry run may require a persisted refresh before a detail
can resolve the ID. Opening or scanning the catalog does not request a model draft.

For a handoff naming one ID, use `rl mem detail ID --json` with its supplied registry.
Check the returned `memoryId`, `fingerprint` and `incarnation` against the handoff before
asking for a decision. If they differ, or detail cannot read that revision, report the
change and stop the scoped handoff. For a general review, refresh and read the new
revision before asking for a new decision. Never substitute current guards for an old
approval. Keep native memory commands in the review working directory for the same
settings context. The user chooses the save destination independently.

Read all of `record.content`, including both parts of a Codex stage-1 record. Use name,
type, source, native location and project/global scope to orient the user. Distinguish
an empty complete store, an absent store, partial or unavailable observations, and an
unverified or unsupported harness. Cached rows can survive an incomplete scan. Report
`mutationBlock` and leave that item alone. Keep full entries as units of choice, even
if several entries cover the same subject.

## Approved actions

In the commands below, **REVIEW** stands for
`--if-fingerprint DIGEST --if-incarnation CYCLE` from the exact detail record. Include
`--json` for structured outcomes and the same registry when one was supplied. Use
argument arrays or safely quoted values. Keep approved Markdown in a private temporary
file or send it as literal stdin (`--from-file -`). Clean up temporary drafts at the end.

- **Defer.** Leave the source, queue and decision history alone.
- **Discard.** Explain that the source entry will be removed without a harvest. After
  explicit approval for that entry, use `rl mem delete ID REVIEW`.
- **Harvest into the shared queue.** Draft from the full entry or offer the original text
  as a starting point. If the user wants the configured model to draft, explain the model
  call first and use `rl mem harvest ID --dry-run REVIEW --json`. Automatic drafting
  requires a tool-free completion backend. When it is unavailable, use the native
  text or prepare a draft during this reviewed handoff. Let the user edit and
  approve the exact Markdown. Use `rl mem harvest ID --from-file FILE REVIEW` to save only.
  With approval for both steps together, use `rl mem complete ID --from-file FILE REVIEW`.
  Cancelled, empty and failed drafts leave the source intact. Follow the queued-draft
  checks below for split removal or replacement.
- **Integrate directly.** Confirm the user's chosen method and living-doc destinations
  before editing a clean, explicitly chosen checkout. Use their standing `/doc` choice
  when applicable and keep the edits uncommitted. Use living docs as publication
  targets and exclude always-loaded instructions, rules and skills, including the
  physical targets of linked rule files and directories. After the
  user confirms the exact approved text was integrated, run
  `rl mem mark harvested ID --from-file FILE --paths DEST ... REVIEW` with the files
  that now exist. This records the user's attestation and checks them. Then run `rl mem complete ID REVIEW` when
  approved. Completion rechecks destination evidence. Missing or changed evidence
  leaves the source in place. Use this route only when the user chose direct integration.

### Queued draft approval

Read `rl harvests snapshot --json` with the same registry. Match one memory item to the
reviewed ID, source, fingerprint, incarnation, scope and source project. Verify that its
`approvedBodyDigest` equals the SHA-256 of the exact approved Markdown bytes. Use the
`token` from that same row. Keep the immutable `harvestId`, origin, digest and token with
the approval. When the user approves removal of that queued draft, run
`rl mem complete ID REVIEW --if-harvest-token TOKEN`. If the token changes, check the
reviewed origin and exact body again. A different body needs fresh approval. If saving
was interrupted, recover the save before reviewing the current queue draft.

For replacement, use the token paired with the saved draft shown for review before the
edit. If the exact saved draft is needed, `rl harvests --json --registry PATH` supplies the body
from the same registry’s pending queue. Match
its full origin and SHA-256 against the snapshot row before taking the token. When the
user approves replacing it with the new Markdown, pass that token as
`--if-harvest-token` with the review guards. A stale token means the saved draft must be
checked again before replacement. To complete after a successful replacement, obtain the
token and digest for the newly approved body together from a new snapshot.

The shared queue can retain a draft without a destination. If the user chooses an
existing checkout, take the current token from
`rl harvests snapshot --json --registry PATH` and use
`rl harvests assign TOKEN --project CHECKOUT --registry PATH`. Use the returned token
for the assignment's replacement of the previous token. The source scope and project
are provenance. The chosen checkout is separate. Use `ringleader-publish` when the user
chooses to publish from the queue. Pass the exact selected tokens, registry, checkout
scope and chosen adapter. Its normal receipt and local commit rules apply, including
explicit acceptance for material already covered or deliberately needing no doc change.

### After publication

If the exact approved draft was already published, read the durable
`memoryPublications[harvestId]` record in the same registry. Check the immutable ID, full
memory origin and `approvedBodyDigest` against the approval, and confirm the recorded
changed or accepted no-change outcome. Only then use `rl mem complete ID REVIEW` without a
queue token. Pause if evidence is missing or mismatched. Queue disappearance alone cannot
establish publication.

## Pending and recovery

Explain the applicable recovery policy when requesting removal. The default is 30 days
from completed source removal. A policy of 0 purges the recovery payload as soon as
removal completes. A saved removal approval retains its exact draft and original policy
on retry.
This is independent of session archive retention.

If a result is `removal-pending`, report its operation ID and reason even when the
command exits successfully. Usable presence coverage and a quiet native store are
required. For a coverage problem, compare the expected and reported hook paths in the
outcome. The agent running this skill can itself keep a store busy. After it is quiet,
use `rl mem recover OP_ID` for unfinished removal or to check deferred Codex
consolidation. If the retry requires a fresh review, get the new exact detail and
confirm again. Re-trying a busy restore uses `rl mem restore OP_ID`. Codex's selected
row and its supported input artifacts are distinct from mixed derived summaries.
Consolidation is performed by Codex later and can remain pending or fail.

Use `rl mem recovery` to show operation status and available backups. Restore only the
selected operation with `rl mem restore OP_ID`; a collision refuses and a successful
restore creates a fresh review cycle. Published docs and queued drafts are independent
of restoration. Expired backups are removed through a user-approved
`rl mem recovery prune --delete` after preview with `rl mem recovery prune`.
Never run bulk live-memory pruning or edit native files, databases or the registry by
hand. Report entries left undecided, approvals saved, removals completed or pending,
and any unresolved destination or recovery work.
