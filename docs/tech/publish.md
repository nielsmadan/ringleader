# Harvest store and publishing

`harvests.py`, `publish_state.py`, `publish_git.py`: durable knowledge from finished sessions,
and the guarded path that writes it into a repository.

## Purpose

A harvest is markdown distilled out of a session before it is archived. It lands in a
machine-local queue first, and is only later *published* into a repo's living documentation.
Publishing edits someone's git checkout, so the write path is validated and recoverable rather
than direct.

## How it works

### The harvest store

`harvests_dir()` is `~/.local/share/ringleader/harvests/`. Each harvest is a markdown file with
frontmatter, parsed into a `Harvest` by `_parse_harvest()`.

Frontmatter values are validated against `_FRONTMATTER_UNSAFE` — a harvest is model-generated
text, so characters that would break out of the frontmatter block are rejected at write time.
Files that fail to parse become `HarvestProblem` entries rather than exceptions, so one bad
harvest does not take down `rl harvests`.

`snapshot_harvests()` returns a `HarvestSnapshot` of the good and bad together, which is what the
inbox renders.

### Publish receipts

`publish_state.py` exists because publishing is a multi-file write into a git checkout that can
fail halfway. A `PublishReceipt` records the intended write set — `ReceiptItem` per file, each
with a `harvestId`, `digest`, `token`, `sessionId` and `relativePath` — so an interrupted publish
can be resumed or rolled back rather than leaving a partial edit.

Receipts live under `paths.publish_receipts_dir()` and are written under `stateio.publish_lock()`.

Validation is deliberately strict, because the receipt drives writes into a user's repo:

- `_validate_repository_relative_path()` rejects absolute paths and any traversal, so a receipt
  cannot address a file outside the checkout.
- `_OID` requires a full 40- (or 64-) character git object id; abbreviations are refused.
- `MAX_ITEMS` (100) and `MAX_BYTES` (128 KiB) cap a receipt's size.

### Git safety

`publish_git.py` inspects the target checkout before writing. `CheckoutState` captures what
matters, and the guards refuse to proceed when:

- an operation is in progress — `_OPERATIONS` covers `MERGE_HEAD`, `rebase-merge`,
  `rebase-apply`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`;
- the target basename is in `_DISALLOWED_BASENAMES` (`agents.md`, `claude.md`) — the
  always-loaded instruction files are never written by an automated harvest publish.

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
