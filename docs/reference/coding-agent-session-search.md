# Coding Agent Session Search (cass) and ringleader

Captured 2026-10-13 from the 2026-10-12 review of upstream `main` documentation and
resume source. No release version was pinned; cass was not installed or benchmarked.
Recheck upstream before using this comparison to make an integration decision.

[Cass](https://github.com/Dicklesworthstone/coding_agent_session_search) centers on
a preserved, searchable conversation archive. Ringleader centers on unfinished work,
session lifecycle decisions, and continuation in the same or another agent.

## Overlap and differences

| Area | Cass | Ringleader |
|---|---|---|
| Search | Indexed transcript contents, Boolean and phrase queries, optional local semantic search | Names, IDs and cached blurbs; fuzzy filtering in the resume picker |
| Agent coverage | Broader connector inventory, including Cursor, Gemini, Aider and many others | Claude Code, Codex, Droid, OpenCode, Pi and Cursor CLI |
| Resume | Detects the originating harness and constructs its native resume command | Same-agent resume plus native cross-agent conversion, forks and handoffs |
| Curation | Tool-readiness preflight, bookmarks and archive maintenance | Name, classify, snooze, keep, finish, archive and prune sessions |
| Preservation | Copies conversation content into its own canonical database; source disappearance need not erase that history | Moves supported file-backed sessions out of native resume pickers, with unarchive to restore them |
| Knowledge reuse | Cited, token-budgeted context packs; lesson retrieval from commits, closed beads and proof manifests | Distills sessions into harvests and publishes them into repository documentation |
| Inspection | Transcript viewer, context expansion, bookmarks, HTML export and analytics | Session overview/inbox, transcript exports, token usage and subscription windows |
| Multiple machines | SSH/rsync ingestion with provenance and remote-to-local path mappings | Usage-report merging; workspace moves and native session-storage rebinding |

Ringleader's current behavior is detailed in [resuming sessions](../features/resuming-sessions.md),
[session lifecycle](../features/session-lifecycle.md),
[harvest and publish](../features/harvest-and-publish.md), and
[workspace portability](../features/workspace-portability.md).

## Similar names, different operations

- **Triage:** cass documents its command as readiness preflight for agents using cass.
  Ringleader's triage is the queue of dormant sessions awaiting a lifecycle decision.
- **Forget:** cass says, “It removes indexed copies, not source files”. Its raw mirror
  is managed separately. Ringleader's delete/prune operations affect actual session data.
- **Archive:** cass's logical archive commands export, verify, search and restore canonical
  database content. Ringleader archive changes whether an original session appears in its
  agent's native picker. OpenCode lifecycle mutation is currently excluded.
- **Resume:** cass does have native resume dispatch. Its `--agent` is a harness-detection
  override; the inspected implementation does not translate transcript formats as
  `rl resume --to` does.
- **Path mappings:** cass rewrites remote paths for local viewing. Ringleader's workspace
  move/retarget changes directory locations and supported native session storage or metadata.

## Possible complement

Cass could supply full-transcript retrieval for questions such as “where did we solve this
before?”, while ringleader manages which work to continue, finish or remove. Bounded, cited
context packs are another useful capability to study. No cass dependency or integration was
adopted by this review, and no equivalent to ringleader's harvest-to-repository publication
workflow was established in the inspected material.

The advertised sub-60 ms cass search figure measures its engine query, not total CLI latency.
It is not comparable to the [ringleader picker benchmark](../tests/picker/README.md), which
measures ranking and result replacement on a different workload.

## Upstream evidence

- [Search and UI features](https://github.com/Dicklesworthstone/coding_agent_session_search#-key-features): lexical/semantic retrieval, connectors and the transcript viewer.
- [Core commands](https://github.com/Dicklesworthstone/coding_agent_session_search#core-commands): readiness triage, context packs, lessons, forget and logical archives.
- [Session analysis](https://github.com/Dicklesworthstone/coding_agent_session_search#session-analysis-commands): discovery, export, context expansion and timelines.
- [Remote sources](https://github.com/Dicklesworthstone/coding_agent_session_search#-remote-sources-multi-machine-search): SSH/rsync ingestion and path mappings.
- [Resume implementation](https://github.com/Dicklesworthstone/coding_agent_session_search/blob/main/src/lib.rs#L111818-L111995): native resume argv resolution inspected during the review; line anchors may move with upstream changes.
