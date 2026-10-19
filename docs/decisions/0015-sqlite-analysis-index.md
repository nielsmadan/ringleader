# 0015: Keep session analysis in a derivable SQLite index

## Context

`rl analyze` answers questions over individual tool calls, requests, user messages and
compactions: error rates by tool, cache hit rates by model, the worst sessions by penalty per
100 calls, recurring error signatures across sessions. Those are per-call rows and grouped
queries over a corpus of about 11 GB of transcripts. The JSON-file pattern used for the rest of
ringleader's state (0001, 0010) loads and rewrites a whole document per run. That does not scale
to millions of rows, and it gives no grouped queries.

The state dir is shared. Eight dev checkouts plus the installed `rl` read and write the same
`~/.local/share/ringleader/`, often at different versions, and an agent session can run
`rl analyze` while the user runs it in a terminal.

## Decision

**A derivable SQLite cache.** Analysis facts live in `analysis/index-v{FACTS_VERSION}.sqlite`
under the state dir (`--index DIR` overrides it). Everything in it can be rebuilt from the
transcripts, so the file is a cache: deleting it costs a cold build and nothing else.

- **Versioned filenames.** A change to the facts schema or extractor bumps `FACTS_VERSION` and
  so names a new file. A run deletes only older-version files, and only under the write lock. It
  never touches a newer one and never unlinks or renames the live file.
- **A derive layer recomputable from facts.** Signals, episodes and thread stats carry their own
  `DERIVE_VERSION` in `meta`. A mismatch drops and recomputes that layer from the stored fact
  rows in one transaction, with no transcript I/O.
- **Incremental ingest.** Each transcript is re-extracted whole when its stat fingerprint
  changes (for a Claude subagent, combined with its `.meta.json`). Vanished transcripts are
  dropped unless their root is unavailable.
- **Query-time dedupe.** Rows that repeat across transcripts (resumed or copied history) are
  stored as found and deduplicated by key when queried, so ingest stays per-file.

**A separate labels store.** System-1 model labels go in `analysis/labels.sqlite`, keyed by
episode identity, content hash and prompt version. They cost model calls and cannot be
re-derived, so an index rebuild never touches them, and v1 never garbage-collects them.

**Concurrency without a coordinator.** The index runs in WAL mode with `synchronous=NORMAL`.
Writers take an fcntl lock (`analysis/index.lock`) and open every transaction with
`BEGIN IMMEDIATE`. Readers open `mode=ro` and never `immutable=1`. A run that finds the lock busy
reads the existing complete index and marks it stale rather than waiting; it waits only when no
usable index exists. A transcript that fails to extract or write gets a failure row keyed by
its fingerprint, is skipped until that fingerprint changes, and never leaves partial rows.
`meta.complete_at` marks a finished build, and a cold build commits in batches so an interrupted
one resumes.

## Consequences

- `FACTS_VERSION` and `DERIVE_VERSION` bumps are enforced by pinned hashes of the DDL,
  taxonomy tables, caps and thresholds, so changing one without bumping fails the suite. A
  change to extractor *logic* is not covered by the hash and needs a manual `FACTS_VERSION` bump.
- Mixed rl versions sharing a state dir rebuild in turn: a newer rl prunes the older file, and an
  older rl then cold-builds it again. Each keeps working, at the cost of repeated cold builds.
- Checkouts with different `DERIVE_VERSION`s share one file and take turns recomputing the derived
  layer, in either direction. That costs no transcript I/O, but a downgrade leaves the newer
  checkout's `--no-refresh` refusing until its next full run.
- A WAL database cannot be opened `mode=ro` in a directory the process cannot write once the last
  writer has closed cleanly, because the reader has no `-shm` to create. Reading from an unwritable
  index dir therefore works only while `-wal`/`-shm` files remain.
- A transcript whose derived rows fail to compute keeps its facts, gets a failure row and no
  derived rows, and is retried at the next `DERIVE_VERSION` bump.
- A cold build reads the whole corpus, about 11 GB today.
- Parsing uses the stdlib `json` module unless the calibrated cold build exceeds 5 minutes.
- This is the first non-JSON state in ringleader. It sits beside the registry and does not
  replace it: decisions and history stay in `registry.json`.
