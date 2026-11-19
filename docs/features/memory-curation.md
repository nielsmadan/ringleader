# Curate native agent memory

The Memories inbox (`8`) and `rl mem` review native Claude Code and Codex auto-memory.
Each review covers one complete Claude topic or inline entry, or one Codex stage-1 record
with its raw memory and rollout summary. Capability and store diagnostics distinguish an
unsupported harness, a missing store, an incomplete scan and a complete empty store.
See [native harness evidence](../reference/agent-memory-management.md).

- **Harvest** keeps editable, approved Markdown in the shared [Harvests queue](harvest-and-publish.md)
  or integrates it directly at the user's chosen destination. Source removal requires
  durable saved knowledge. A global or unresolved entry can stay queued until the user
  chooses its publishing checkout.
- **Discard** authorizes removal of one reviewed native entry without a harvest.
- **Defer** keeps the entry pending. Legacy reviewed and cleaned acknowledgements do
  not replace a decision.

The bundled `ringleader-memory-triage` skill guides the same decisions in an agent.
In the inbox, drafts and discovery run in the background. Read-only mode allows discovery,
full detail and diagnostics while keeping persisted state unchanged. Automatic
drafting supports a tool-free Claude CLI or direct `llm` completion. With Codex CLI
or another backend that cannot establish tool-free completion, edit the native text
with `e`, supply reviewed Markdown with `--from-file`, or choose the exact-entry
skill handoff. A queued draft can contain up to 128 KiB of UTF-8 Markdown. If an
inbox submission fails or is undone, open Memories and press `v` to recover the
submitted wording before leaving the inbox.

Native writes check the reviewed revision and relevant live activity. A durable decision
can be waiting on native removal. Codex can also have finished selected-source removal
while awaiting later consolidation of its derived summaries. Recovery shows these
states separately. A pending removal retains the approved text for retry. Queue and
publication evidence belong to the registry that approved the harvest. Use the same
`--registry` for review, publishing and recovery. Each removal has a recovery window, 30 days by default, with explicit
expiry cleanup. Restoring an available copy creates a fresh review cycle independently
of publication.

Entry points are `commands/mem_scan.py`, `commands/mem.py`, `home/screens/memories.py`
and `skills/ringleader-memory-triage/`. See [memory architecture](../tech/memory.md)
and the [memory JSON contract](../tech/json-contract.md#native-memory-json-version-1).
