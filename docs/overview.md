# Documentation

Map of the `docs/` tree. `README.md` (repo root) is the authoritative user-facing manual;
`AGENTS.md` holds agent/contributor guidance. These docs go deeper, split by
**audience and format**.

## Two audiences

- **Using ringleader** (you run `rl`): start at `README.md`; deeper walkthroughs in
  [`user/`](user/index.md). Verbose, task-oriented, example-driven.
- **Building ringleader** (contributors + coding agents): [`features/`](features/overview.md)
  for *what* each feature does (a terse behavior reference) and [`tech/`](tech/overview.md) for
  *how* it's implemented. Concise and symbol-first, without brittle numeric source anchors.

## Sections

- [user/](user/index.md): how-to guides for people running `rl` (first setup, the triage loop,
  finishing and converting sessions). README links here. Human-format; agents don't load these by default.
- [features/](features/overview.md): what each feature does, kept in sync with the code. The
  behavior reference for contributors and agents. Owned by the `doc` skill.
- [tech/](tech/overview.md): module internals, the scan/reconcile data flow, and the
  cross-cutting patterns (the `Source` seam, the registry-as-cache contract, timestamps at the
  edge). Owned by the `doc` skill.
- [decisions/](decisions/overview.md): append-only records of cross-cutting choices and their
  tradeoffs. Current behavior remains in `features/` and `tech/`.
- [reference/](reference/agent-session-management.md): how the **external** agent CLIs store and
  index sessions — verified findings stamped with date and version. Not our code; it goes stale
  when an agent ships, not when we refactor.

## Layering

`features/` (what it does, tracks code) → `tech/` + source in `ringleader/` (how). `user/` is the
human-facing view of the same behavior, in walkthrough form. `decisions/` records why
cross-cutting constraints were adopted, and `reference/` records what the agents around us
actually do. `doc --update` keeps `features/` and `tech/` in sync with code; `reference/` is
re-verified when a dependency moves, not when we change.

## A note on state

Nothing under `docs/` describes machine-local state. `registry.json`, `archive/`, `harvests/`
`publish/`, and `conversion/` live under `~/.local/share/ringleader/` (`$XDG_DATA_HOME/ringleader` if set) and
are never committed — see [tech/registry.md](tech/registry.md).
