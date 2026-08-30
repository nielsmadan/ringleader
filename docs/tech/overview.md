# Technical / Architecture

How ringleader is built — module internals, the scan-to-registry data flow, and the
cross-cutting patterns that constrain the package. This is the implementation layer; for what
each feature does for the user, see [`../features/overview.md`](../features/overview.md).
`AGENTS.md` (repo root) is the canonical contributor summary; `CLAUDE.md` imports it. These docs
go deeper, per subsystem.

## Module docs

- [registry.md](registry.md) — `registry.py`: the one state file, its schema, the three
  field-refresh rules, and the reconcile contract.
- [sources.md](sources.md) — `sources/`: the `Source` protocol and the opt-in capability
  protocols that model each agent's quirks.
- [scanning-and-refresh.md](scanning-and-refresh.md) — `scanner.py`, `refresh.py`, `gitinfo.py`,
  `projects.py`, `slug.py`: the scan loop, git-project identity, selectors, and Claude Code's
  reproduced path slug.
- [cli-and-commands.md](cli-and-commands.md) — `cli.py` + `commands/`: the nested dispatch table
  and the one-module-per-verb convention.
- [usage.md](usage.md) — `usage/`: four per-harness readers, aggregation, pricing, subscription
  windows, and cross-machine merge.
- [models.md](models.md) — `models/`: the `Model` seam and `active_model()` resolution.
- [publish.md](publish.md) — `harvests.py`, `publish_state.py`, `publish_git.py`: the harvest
  queue and the guarded write path into a repository.
- [integration.md](integration.md) — `commands/skills.py`, `hooks.py`, `statusline.py`,
  `session_env.py`, `safe_write.py`: editing four other programs' configs, removably.
- [inbox.md](inbox.md) — `inbox/app.py`: the Textual TUI.

## Data flow (end to end)

`sources/*.discover()` (stat-only refs) → `scanner.scan()` — which consults
`registry.cached_session()` and calls `parse()` only on a fingerprint miss, then attaches git
identity from `gitinfo.resolve_git_project()`, memoized per cwd → `registry.reconcile()` merges
into `registry.json` → `overview.py` / `commands/triage.py` / `prune.py` read and render.

`refresh.auto_refresh()` is the scan+reconcile+persist wrapper the views call; `--no-scan` skips
it. Alongside: `models/` supplies classification and harvesting on demand, `harvests.py` queues
what they produce, `publish_*.py` drains it into a repo, and `usage/` runs an entirely separate
read path over the same transcripts for token accounting.

## Cross-cutting patterns (read before editing any module)

- **The registry is a cache with a decision layer.** Metadata fields are refreshed freely; git
  fields are refreshed only when a scan actually resolved them; judgment and decision fields are
  never touched by a scan. Getting this wrong is how user decisions get silently lost. See
  [registry.md](registry.md#thread-fields).
- **Timestamps are generated at the edge.** The CLI entry point produces `now_iso` and passes it
  into scan/reconcile, keeping that logic pure and deterministic. Do not call `datetime.now()`
  inside it.
- **Capabilities are asked for, never assumed.** A source advertises a string in `capabilities`
  and satisfies a `@runtime_checkable` protocol; call sites check both and degrade to a no-op.
  Adding an agent must not require touching the registry.
- **State lives outside the repo,** resolved through `paths.py` on every command
  (`~/.local/share/ringleader/`, or `$XDG_DATA_HOME/ringleader`). `paths.py` is the only module
  that knows where anything lives; `ensure_state_dir()` is the write-path accessor, and the
  plain accessors deliberately do not create directories as a side effect.
- **Anything that edits a file we don't own goes through `safe_write` / `stateio`.** Back up,
  preserve mode, replace atomically, and preview a diff before writing. That covers the registry
  itself, agent settings files, and the status line.
- **Destructive verbs preview by default.** `prune` and `mem prune` require `--delete`;
  `workspace move` confirms; installers show a diff. The flag is the action, not the preview.

## Why this shape

Python for a tool whose whole job is reading JSONL that agent CLIs already write locally; one
JSON registry rather than a database because the state is small, inspectable and hand-fixable; a
`Source` protocol because the interesting differences between agents are behavioral (separate
indexes, launch-directory keying) rather than syntactic, and a format-only abstraction would not
have covered them.
