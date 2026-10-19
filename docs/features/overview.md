# Features

**What each ringleader feature does**, kept in sync with the implementation. This is the
behavior reference for contributors and coding agents *building* ringleader: terse and
symbol-first, not a usage tutorial. For human how-to walkthroughs see
[`../user/`](../user/index.md); for internals see [`../tech/`](../tech/overview.md). Owned by the
`doc` skill; run `doc --update` after feature work to keep it current.

## Features

- [session-lifecycle.md](session-lifecycle.md) — the states a session moves through and the verbs
  that reach them: classify, snooze, keep, harvest, finish, sweep, archive, unarchive. Why
  `finish` archives nothing and why it refuses an unharvested session.
- [views-and-triage.md](views-and-triage.md) — the read surfaces (`overview`, `triage`),
  the `inbox` home TUI, git-project grouping, selectors, and `roundup`.
- [harvest-and-publish.md](harvest-and-publish.md) — distilling a session into durable markdown,
  the pending queue, and the guarded path that writes it into a repo.
- [resuming-sessions.md](resuming-sessions.md) — `rl resume`: the cross-agent picker, the
  three directory scopes, and handing a session to a different agent.
- [pruning-and-retention.md](pruning-and-retention.md) — deleting tiny/stale sessions, archive
  retention, and tombstones. Which verbs preview and which delete.
- [usage-analytics.md](usage-analytics.md) — token spend across five harnesses, per-session
  breakdowns, subscription windows, and cross-machine merge.
- [analysis.md](analysis.md) — `rl analyze`: failure signals, session health, cache rebuilds, recurring
  issues, optional labels, and the reports the `analyze` and `recommend` skills write.
- [session-portability.md](session-portability.md) — native conversion, forks, resume, handoff,
  lineage, deterministic compact conversion, and crash recovery.
- [workspace-portability.md](workspace-portability.md) — moving a checkout without stranding its
  sessions; why the registry has to be pinned and why a move refuses to merge.
- [agent-integration.md](agent-integration.md) — the nine bundled skills, the session-end hook,
  the status-line capture, and the ownership rules that keep uninstall safe.
- [memory-curation.md](memory-curation.md) — the parallel `rl mem` track behind the
  `memory-triage` skill.

## Where the behavior lives

| Feature | Implementation |
|---|---|
| [session-lifecycle](session-lifecycle.md) | [registry](../tech/registry.md), `commands/finish.py`, `sweep.py`, `archive.py` |
| [views-and-triage](views-and-triage.md) | [scanning-and-refresh](../tech/scanning-and-refresh.md), [inbox](../tech/inbox.md), `overview.py` |
| [harvest-and-publish](harvest-and-publish.md) | [publish](../tech/publish.md), [models](../tech/models.md) |
| [resuming-sessions](resuming-sessions.md) | [resume](../tech/resume.md), [sources](../tech/sources.md) |
| [pruning-and-retention](pruning-and-retention.md) | [registry](../tech/registry.md), `prune.py` |
| [usage-analytics](usage-analytics.md) | [usage](../tech/usage.md) |
| [analysis](analysis.md) | [analysis](../tech/analysis.md) ([calibration](../tech/analysis.md#calibration)), [ADR 0015](../decisions/0015-sqlite-analysis-index.md), `commands/analyze.py`, `analysis/` |
| [session-portability](session-portability.md) | [session-portability internals](../tech/session-portability.md), `portability/`, `commands/conversion.py` |
| [workspace-portability](workspace-portability.md) | [sources](../tech/sources.md), [scanning-and-refresh](../tech/scanning-and-refresh.md) |
| [agent-integration](agent-integration.md) | [integration](../tech/integration.md) |
| [memory-curation](memory-curation.md) | [registry](../tech/registry.md), `memory.py` |

> The command surface itself — every flag, every default — is a *user* reference and lives in
> [`../user/cli.md`](../user/cli.md), not here.
