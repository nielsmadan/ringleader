# Harvest store and publishing

`harvests.py`, `publish_state.py` and `publish_git.py` take approved knowledge from
sessions and native memory entries through publication into a repository.

## Purpose

A harvest contains Markdown distilled from a session or approved during review of a
native memory entry. It enters a machine-local queue before publication into a repo’s documentation.
Publishing edits someone's git checkout, so the write path is validated and recoverable rather
than direct.

## How it works

### The harvest store

`harvests_dir()` is `~/.local/share/ringleader/harvests/`. Each harvest is a markdown file with
frontmatter, parsed into a `Harvest` by `_parse_harvest()`.

New memory harvests use their opaque immutable ID as the filename. The private
frontmatter holds their title and exact owning registry path. Existing names and tokens
remain valid. If an older record could name two registries because of trailing whitespace,
publication needs the matching journal operation, full memory origin and approved body
digest. Restore the original journal from backup if ownership cannot be established.
Other explicitly owned items still pass through the requesting registry’s owner filter.
Memory reads and new receipts use the same registry as the approval. Clone metadata
strips HTTP credentials, URL queries and fragments, and SSH passwords.
When an older harvest has a URI clone locator, its public repository key is derived
from the sanitized locator on read and on write. SSH and SCP routes keep their login. For SCP and local paths, literal `#` and `?`
characters remain part of the repository name.

Frontmatter values are validated against `_FRONTMATTER_UNSAFE` — a harvest is model-generated
text, so characters that would break out of the frontmatter block are rejected at write time.
Files that fail to parse become `HarvestProblem` entries rather than exceptions, so one bad
harvest does not take down `rl harvests`.

`snapshot_harvests()` returns a `HarvestSnapshot` of the good and bad together, which is what the
inbox renders.

### Publish receipts

`PublishReceipt` records the intended write set so publishing can resume after an interruption.
Each `ReceiptItem` carries a `harvestId`, `digest`, `token`, and `relativePath`. Session receipts
identify the origin with `sessionId`. Memory receipts carry the immutable memory origin,
including its source scope and project, and the SHA-256 `approvedBodyDigest` of the queued Markdown.

The memory body digest survives destination assignment, which changes the queue token. Successful
consumption records the digest in `memoryPublications`, where memory completion uses it to verify
the exact approved draft. Older memory receipts can finish publication. For an
already-issued receipt naming a different registry, a unique lifecycle journal must
prove the harvest ID, complete native origin and approved body digest. Before unlinking
the queue file, the finisher records the proven owner alongside that item’s origin,
body digest, token and path in the private finishing receipt. A retry can publish to
the same owner after journal loss. If a queue file survives an interrupted receipt
write, retry syncs the receipt directory before unlinking it. A redirect of the recorded
canonical registry path or conflicting surviving evidence stops consumption.
An older finishing receipt with no provable owner stays pending until its original
journal or queue file can be restored from backup. A same-owner publication records the
exact user-selected queue body. Native removal independently requires that digest to
match the lifecycle-approved Markdown.

Receipts live under `paths.publish_receipts_dir()` and are written under `stateio.publish_lock()`.

Validation is deliberately strict, because the receipt drives writes into a user's repo:

- `_validate_repository_relative_path()` rejects absolute paths and any traversal, so a receipt
  cannot address a file outside the checkout.
- `_OID` requires a full 40- (or 64-) character git object id; abbreviations are refused.
- `MAX_ITEMS` (100) and `MAX_BYTES` (128 KiB) cap a receipt's size.

Unassigned memory remains in the pending queue until the user selects a checkout.
`rl harvests assign` verifies the exact token, destination Git identity and receipt claims,
then reconciles the replacement token against the immutable harvest ID. Source scope
and project are immutable provenance. Queue readers surface conflicting and already
published copies as problems. A normal approved memory save reconciles the whole
queue. A superseded terminal operation can reproject metadata idempotently, without
replaying its native transaction. Source backup expiry and restoration are independent
of queue consumption.

The receipt and commit protocol covers both origins. Only session origins participate
in session lookup or archival. Memory can be published before source completion.
The user can also choose direct integration outside the publisher, with approved
Markdown and destination evidence recorded before removal and edits left uncommitted.
See [memory approvals](memory.md#approvals-and-journal).

### Git safety

`publish_git.py` inspects the target checkout before writing. `CheckoutState` captures what
matters, and the guards refuse to proceed when:

- an operation is in progress — `_OPERATIONS` covers `MERGE_HEAD`, `rebase-merge`,
  `rebase-apply`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`;
- the target basename is `AGENTS.md` or `CLAUDE.md`;
- a receipt includes memory and a changed path or its resolved target is an agent
  instruction file or area. The guard uses the same basenames and config directory
  names as native memory integration, including instruction directories under the
  active native configuration roots. A metadata-only traversal collects the physical
  targets of protected files and directories, including linked rules. A configuration
  directory reached through a checkout alias also reserves prospective instruction
  children before an editor creates them. Memory and mixed
  bundles list their checkout-relative aliases. The adapter excludes these paths and
  their descendants before editing. Prepare and direct integration recheck physical
  targets. Direct integration outside Git checks the destination directory or the
  enclosing project configuration and active instruction roots. A document directly
  under the user home does not require a recursive home inventory. An unreadable tree or traversal above the entry or alias-size limit
  prevents verification. An external process can still change filesystem links
  between checks. See the [publishing protocol](../../skills/ringleader-publish/references/protocol.md#begin-and-adapter).

## Key entry points

- `harvests.snapshot_harvests()` / `load_harvests()` — read the queue.
- `harvests.write_harvest()` — add to it.
- `publish_state` — `PublishReceipt`, `ReceiptItem`, `ConsumeResult`, `PublishStateError`.
- `publish_git.CheckoutState` — the pre-write inspection.
- `stateio.publish_lock()` — serializes receipt mutation.

## Gotchas

- **`harvestPath` is an array and may mix two kinds of path.** Tool-native harvesting records
  pending *queue item* paths; direct agent-driven integration records the *living document*
  paths it wrote. Reconciliation replaces or clears only the queue entries, using the harvest
  store root to tell them apart — living-document paths survive.
- **The queue is durable only until integration.** A published harvest leaves it; it is a
  work queue, not an archive of everything ever learned.
- **`AGENTS.md` and `CLAUDE.md` cannot be publish targets.** Refused by basename, case-folded.
- **A malformed harvest degrades to a `HarvestProblem`,** surfaced in listings rather than raised.

## Why

Receipts and pre-write git inspection are heavier than "write the files", and the weight is the
point: this is the one code path that modifies a repository the user cares about, on the basis of
model-generated text. Refusing mid-rebase and capping the write set makes the failure mode a
declined publish rather than a corrupted checkout.

## Related

- [features/harvest-and-publish.md](../features/harvest-and-publish.md)
- [0006: harvest before finish](../decisions/0006-harvest-before-finish.md)
