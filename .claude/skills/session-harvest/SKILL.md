---
name: session-harvest
description: Harvest one dormant Claude Code session's knowledge into its repo's docs — extract gotchas, decision rationale, and what-changed, and integrate them via the /doc skill. Use when a finished/old session has useful info worth keeping before you stop tracking it. Does not delete or archive the session.
---

# session-harvest

Capture the durable knowledge from a single session into the product's docs. The
deterministic bits are ringleader CLIs; the doc integration is the `/doc` skill.

## Steps

Requires `rl` installed (`just install-local`) or, from a source checkout, run each
command as `uv run rl <sub>`. You need the session's `sessionId`. Get it from the
registry (`~/.local/share/ringleader/registry.json`): each entry in `.threads[]` has
`sessionId`, `project`, and `whatItIs`. `rl overview --all` and `rl overview <repo>`
are human-readable summaries and do **not** print full ids.

1. **Export the conversation** to a temp markdown file:

   ```bash
   rl data export <id> --out /tmp/rl-harvest-<id>.md
   ```

2. **Integrate into the repo's docs** by invoking the `/doc` skill with the export, from
   the *target repo's* working directory (the session's project). Pass a report file:

   `/doc --session --md /tmp/rl-harvest-<id>.md --report /tmp/rl-harvest-<id>.report`

   `/doc` previews the doc edits; confirm them with the user. It writes the list of
   touched doc paths to the report file.

3. **Record the harvest** in the registry (paths come from the report file, comma-joined):

   ```bash
   rl session mark harvested <id> --paths "<comma-joined paths from the report>"
   ```

   If the report is empty (nothing worth documenting), run `rl session mark harvested <id>` with
   no `--paths` — it still marks the session harvested.

4. **Report** which docs were updated and that the session is marked harvested.

## Notes
- This harvests only — it does NOT archive or delete. To move a finished session out of
  the resume picker afterward, use `rl session archive <id>`.
- Never hand-edit `registry.json` or transcripts.
