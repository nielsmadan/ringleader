# Native memory architecture

## Discovery and review identity

`memory_scan.configured_sources` constructs the readers in `memory_sources/`.
Their capabilities are independent of the session portability adapters. Native scope
covers Claude Code auto-memory (including persistent subagent notes) and pinned Codex
stage-1 memory. [Native harness evidence](../reference/agent-memory-management.md)
records verified formats and the four other CLI capability limits.

Claude discovery reads user/managed configuration and project settings with native
trust evidence. Default `~/.claude.json` provides trust only with the default config
root. For a custom root, users can select its native trust document through
`memory.claude_trust_file`. A custom root without trust evidence gets a diagnostic
when project settings cannot be considered. Explicit trusted roots are a caller/test
dependency. Discovery follows known project and shared worktree names without
guessing ambiguous provenance. It excludes symlinks and instruction areas. Topic
files and supported inline prose remain recoverable independently of index links.
A note, index or configuration text file can contain up to 8 MiB. A Claude index
can contain up to 65,536 physical lines. An over-budget index needs to be shortened
before curation. Other readable notes remain available. Oversized or
unreadable notes produce partial store observations, preserving the last known rows.
The reader identifies the index by file identity. The CommonMark parser classifies
source lines. Removal accepts standalone topic links in the supported index syntax and
copies retained lines byte for byte, including line endings and any initial BOM.
Guidance and nested bullets must stay with their original list item, and ordered
instructions must keep their numbering. Fenced and indented examples stay intact. A nested fence or a change to the Markdown
interpretation of retained lines stops removal before staging the index or topic.

Codex database selection uses an explicit `memory.codex_sqlite_home` when supplied.
Otherwise it uses provable configured SQLite home, then `CODEX_SQLITE_HOME`, then
`CODEX_HOME`. Conflicting settings or profiles require an explicit root. The memory
artifact workspace lives under `CODEX_HOME`, independently of the database and
`CODEX_SESSIONS_DIR`.

`codex.open_readonly` observes a private, size-bounded capture of the DB and any
existing WAL. The two files together can occupy up to 256 MiB per database capture.
A capture over that budget produces a store diagnostic before copying. Optional
project enrichment reads only the observed thread IDs from the state database and
reports its own diagnostic if unavailable. Healthy memory rows remain available.
The reader checks native identity, size, mtime and ctime across capture and the
query, including WAL appearance or disappearance. SQLite can create sidecars only
against the private copy. A changing database gives a partial/unavailable observation.
The capture excludes native SHM and never uses immutable SQLite mode against live
storage. Scratch selection excludes inspectable native and aliased roots. An unsupported
native symlink behind an unsearchable parent cannot always be identified, so a scratch
path deliberately pointed at its accessible physical target can change directory
metadata during capture. Mutation still refuses unverifiable native paths.

`memory_catalog` keeps the adapter's `nativeIncarnation` separate from the projected
review-cycle `incarnation`. The service verifies the exact public reference before
translating to the adapter's native reference. A scan reads its registry baseline before
native I/O. Only a complete per-store observation can drop missing cached records.
Scan generation and decisions prevent an older scan from resurrecting a removed entry.
A later recreation, restore or observed transition back to older content starts a fresh
cycle. An authoritative inventory retires stores absent from the active configuration.
Their metadata remains cached and drops out of active review. A configuration failure
preserves the previous inventory. Discovery retains metadata for each entry and detail
opens the selected entry to obtain its complete body.
See [registry merge rules](registry.md).

## Approvals and journal

`MemoryLifecycle` is shared by CLI and the inbox Writer. Approval requires the reviewed
fingerprint and incarnation; a queued draft replacement also requires its current token.
Its recovery journal under `memory/operations/<operation ID>/` records choice, retention,
source capture, exact before/after components and progress. A private, revisioned
payload of up to 96 MiB holds recovery text and images. Once it is durable, the
writer atomically publishes a header of up to 128 KiB with the payload revision,
byte length and SHA-256.
Readers list history from headers and verify a selected payload before replay.
Once a header switch is durable, the old payload can be removed. Bulk recovery also
cleans stranded checkpoints. A new operation installs a private staging directory
containing its durable initial marker under the operation ID in a single rename.
Recovery validates the directory ancestry and staged files before cleanup. Without that marker, a missing header leaves ownership unknown even when
the journal directory is empty. A completed save can have a header only. Restore the header from backup before publishing an affected legacy harvest.
A damaged payload is isolated by its header. A legacy
single-file journal remains readable. If its corruption hides ownership and source
lineage, recover the journal from backup before further decisions.

Queue files and publication
receipts own publishing state. The operation references the immutable harvest ID.
`save` reconciles publication metadata for the whole queue during a normal approved
save. Terminal recovery of an already-finished superseded operation only repairs metadata
and succeeds idempotently if another caller repaired it first.

Completion needs the matching queued draft, consumed publication evidence or a recorded
direct integration. CLI and inbox confirmations capture the reviewed reference, approval
operation, evidence route and applicable retention together under the publication lock.
For a queued draft this includes the exact approved text and token. Confirmation releases
the lock for user input. Execution verifies the same evidence under the lock before removal. A replacement uses the token paired with the
saved draft reviewed before editing. Direct integration is a user's attestation with
exact approved Markdown and canonical existing destination file digests. The evidence is
written durably and checked again before removal. It does not require a documentation commit.
Once removal is requested, the journal binds retries to its approved body, including
while presence delays the first native mutation.
Consumed evidence records the approved body digest independently of mutable routing.
A mismatched or unprovable body cannot authorize removal. See [publishing](publish.md).
Memory queue approvals accept up to 128 KiB of UTF-8 Markdown. Direct integration
accepts up to 8 MiB. The queue owner is the canonical registry path. Symlinks to
that path and proven case aliases share ownership across atomic registry replacements.
A copied registry or a separate hardlink directory entry has a different owner. New memory
queue files encode the owner path losslessly, including trailing whitespace. Existing
records with uncertain ownership require unique journal proof. Issued publication
receipts remain recoverable. A newer approval can supersede an earlier decision for
the same physical native entry while it is waiting to stage native work. Once a native
removal or restoration intent is durable, finish or recover it before starting another
approval, even if the journal has recorded no component progress. Shared operation
storage and the publication lock enforce this across registries.

Models and native content are inputs. Automatic drafting passes native text only to
a backend with an explicit tool-free completion capability. The user can edit native
text, supply Markdown, or choose a reviewed skill handoff. Drafting runs outside
mutation locks. A failed,
empty or cancelled draft cannot authorize discard.

Lock order is publication lock, then native workspace lock for Codex, then its SQLite
transaction. Registry projection follows release of the native locks. Quietness requires
usable presence hook coverage at the relevant **active native configuration root** plus
known current-session evidence, including the triaging agent. Missing coverage, active
sessions or a busy lock can leave a durable approval pending. The pending message shows
the expected and reported paths for hook coverage failures. `rl data presence status`
reports general installation status.

`prepare` journals exact native changes before applying them. A retry uses its current
clock for lease checks and the original journaled targets and images. Native DB rows and
jobs commit together. Filesystem components apply separately, each from its validated
before-state or recognized after-state. A newer conflicting source or restore target
refuses. Claude has no cooperative writer lock. The adapter checks presence and exact
file/index identity, holds the validated handle and stages reversibly. Native writes can
still race. On filesystems without reliable birth identity, persisted device/inode
evidence alone cannot authorize unattended replay. Fresh review or the journal's exact
staged file proves continuity.

Codex independently verifies the executable version, native migrations/schema, selected
row, artifact grammar and job/lease state under bounded locking. The approved
incarnation includes the canonical artifact workspace and physical identities of its
root and Codex home. Presence checks, writable access, restore and cleanup recheck
that binding. For old unbound approvals, review a present entry again. For an already
removed entry, preserve its backup and establish the original workspace before
manual restoration. Selected-source removal
keeps a successful no-output stage-1 watermark, removes matching source artifacts and
enqueues later consolidation with a target strictly above both previous watermarks.
Mixed derived summaries and the native Git baseline are left for Codex; there is no
agent launch. Completion of that native work is observed from the successful target
watermark and source artifacts. It is distinct from the durable source removal and
offers no guarantee of semantic forgetting. A genuinely newer source revision can
produce memory again. See [the pinned native format](../reference/agent-memory-management.md#codex).

## Recovery

Use `recover` for an interrupted removal, projection or consolidation check. A busy
restore is retried with `restore`. Recovery restores selected files/links/rows, refuses
collisions and keeps newer valid native watermarks. It never restores a whole database
or rewinds publication.

Expiry starts when selected-source removal completes, even if derived consolidation is
pending. The terminal checkpoint drops full replay images and retains the selected
snapshot for restoration. The initial removal approval records its policy. The TUI displays that policy
on retry and checks it under the publication lock, so a racing approval with a different
policy requires another confirmation. CLI retries retain the journal policy. A save-only
draft can still choose a policy when removal is first approved. Each fresh inbox
confirmation reads the current configured default.

Default retention is 30 days. Zero expires the payload when removal completes, unlike
zero session archive retention, which disables archive expiry. Explicit recovery prune
deletes expired snapshots and all equivalent operation-owned content, including
before/after images and approved direct Markdown. IDs, digests, destinations, disposition
and minimal cleanup receipts survive. Pending harvests, consumed publication evidence
and journals needed by unfinished native mutations survive independently.
