# Technical / Architecture

How ringleader is built — module internals, the scan-to-registry data flow, and the
cross-cutting patterns that constrain the package. This is the implementation layer; for what
each feature does for the user, see [`../features/overview.md`](../features/overview.md).
`AGENTS.md` (repo root) is the canonical contributor summary. These docs
go deeper, per subsystem.

## Module docs

- [registry.md](registry.md) — `registry.py`: the one state file, its schema, the three
  field-refresh rules, and the reconcile contract.
- [session-portability.md](session-portability.md) — deep adapters, canonical preservation,
  snapshots, native publication, lineage, and forward recovery.
- [sources.md](sources.md) — `sources/`: the `Source` protocol and the opt-in capability
  protocols that model each agent's quirks.
- [scanning-and-refresh.md](scanning-and-refresh.md) — `scanner.py`, `refresh.py`, `gitinfo.py`,
  `projects.py`, `slug.py`: the scan loop, git-project identity, selectors, and the reproduced
  path slug Claude Code and Droid share.
- [cli-and-commands.md](cli-and-commands.md) — `cli.py` + `commands/`: the nested dispatch table
  and the one-module-per-verb convention.
- [usage.md](usage.md) — `usage/`: five per-harness readers, aggregation, pricing, subscription
  windows, and cross-machine merge.
- [analysis.md](analysis.md) — `analysis/`, `commands/analyze.py`: the SQLite analysis index,
  signal detection, health, rollups, issues, labels and reports, with the
  [calibration](analysis.md#calibration) record for its thresholds.
- [models.md](models.md) — `models/`: the `Model` seam and `active_model()` resolution.
- [publish.md](publish.md) — `harvests.py`, `publish_state.py`, `publish_git.py`: the harvest
  queue and the guarded write path into a repository.
- [integration.md](integration.md) — `commands/skills.py`, `hooks.py`, `statusline.py`,
  `session_env.py`, `safe_write.py`: editing five other programs' configs, removably.
- [inbox.md](inbox.md) — `home/`, `commands/inbox.py`: the Textual home TUI, its single writer and
  the leave barrier.
- [resume.md](resume.md) — `resume.py`, `picker/app.py`, `commands/resume.py`: selecting a
  dormant session and handing the process to an agent.
- [tui-testing.md](tui-testing.md) — `tui/snapshot.py`, `scripts/tui_fixture.py`,
  `scripts/tui_shot.py`, `tests/tui_e2e.py`: golden screens, text shots, and pty end-to-end runs.
- [gui-contract.md](gui-contract.md) — `gui.py`, `gui_cli.py`, `gui_decide.py`: the versioned
  `rl gui` JSON contract the circus desktop app talks to, its goldens and synthetic world.
- `search.py` / `searchview.py` / `tty.py` — `rl session search`: the matcher (tiers,
  scope, archived-tombstone fallbacks) is kept apart from the renderer that colours it,
  so `jsonout.py` never depends on terminal formatting. `tty.py` owns the `--color`
  policy for whatever command wants it next.

## Data flow (end to end)

`sources/*.discover()` (source-owned refs and fingerprints) → `scanner.scan()` — which consults
`registry.cached_session()` and calls `parse()` only on a fingerprint miss, then attaches git
identity from `gitinfo.resolve_git_project()`, reused per cwd for `GIT_RECHECK_AFTER` (24 h) → `registry.reconcile()` merges
into `registry.json` → `overview.py` / `commands/triage.py` / `prune.py` read and render.

`refresh.auto_refresh()` is the scan+reconcile+persist wrapper the views call; `--no-scan` skips
it. Alongside: `models/` supplies classification and harvesting on demand, `harvests.py` queues
what they produce, `publish_*.py` drains it into a repo, and `usage/` runs an entirely separate
read path over the same transcript files or database rows for token accounting. `analysis/` is a
third read path: it indexes Claude and Codex transcripts into a derivable SQLite cache under the
state dir and answers `rl analyze` from it.

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
- **Configuration edits use `safe_write` / `stateio`.** Back up, preserve mode, replace
  atomically, and preview changes to agent settings and the status line. Registry writes
  use locked bounded mutations. Native session publication has a different contract:
  create-only files, append-only name indexes, schema-gated SQL inserts, and durable
  intents. Never replace a shared native database or truncate an index for rollback.
- **Destructive verbs preview by default.** `prune` and `mem prune` require `--delete`;
  `workspace move` confirms; installers show a diff. The flag is the action, not the preview.

## Why this shape

Python for a tool whose whole job is reading JSONL that agent CLIs already write locally; one
JSON registry rather than a database because the state is small, inspectable and hand-fixable; a
`Source` protocol because the interesting differences between agents are behavioral (separate
indexes, launch-directory keying) rather than syntactic, and a format-only abstraction would not
have covered them.
