# 0002: Build the behavior as skills first, crystallize into a CLI after

## Context

The valuable parts of curation — deciding a session is finished, naming it, distilling what it
taught — are judgment calls, not computations. Writing them as CLI code first would have meant
guessing at the workflow before ever running it, and encoding those guesses in Python.

## Decision

Prototype every capability as an agent **skill** operating directly on
`~/.claude/projects`, then move the deterministic parts into a packaged CLI once the workflow
proved out. The split that settled:

- **The CLI (`rl`) owns the deterministic half:** scanning, reconciling, filtering, moving and
  deleting files, rendering views.
- **The skills own the judgment half:** classifying, naming, harvesting, deciding.

## Consequences

- The skills call `rl <sub>` rather than reading transcripts themselves, so both agree on one
  registry.
- The CLI is testable without a model in the loop; `rl session enrich` and `rl session harvest`
  are the two verbs that reach for one, and both no-op cleanly when none is configured.
- The workflow was validated before it was encoded, and the CLI's command surface follows the
  shape the skills actually needed rather than a shape designed up front.
- Superseded in part by [0007](0007-agent-neutral-skill-packaging.md), which moved the skills
  out of a Claude-only layout.
