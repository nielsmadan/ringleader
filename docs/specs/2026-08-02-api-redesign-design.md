# CLI API Redesign — Design

**Goal:** Collapse ringleader's 20 flat top-level commands into a small set of
daily verbs plus a few subsystem namespaces, so the common path is one word at the
top and related plumbing is grouped and discoverable.

**Architecture:** Three pillars sit on one registry — the **command API** (this
spec), an **LLM capability** (later), and **interactive surfaces / TUI** (later).
Three seams keep external things out of app code: **Source** (where sessions come
from — exists), **Model** (which LLM provider — later), **Target** (where harvested
knowledge lands — later). This spec reorganizes the command surface only; it
introduces no new runtime behavior and no new dependencies.

## Scope

This is **Pillar A: the command reorg**, one of three specs:

- **Spec 1 (this doc):** regroup the existing 20 commands into the new surface;
  update the six skills' call-sites and the README.
- **Spec 2 (later):** LLM capability — implement the reserved `complete`, `enrich`,
  `config` verbs, the Model and Target seams, and settings.
- **Spec 3 (later):** interactive TUI over the enriched registry.

`complete`, `enrich`, and `config` are **named and reserved** here (documented in the
surface, no implementation) so their placement is fixed before Spec 2 builds them.

## Design principles

- **Common commands stay flat.** The daily entry points (`overview`, `triage`,
  `prune`) are top-level, one word.
- **Two symmetric subsystems, each a namespace:** `rl session <sub>` and
  `rl mem <sub>`. The bare flat verbs default to the **sessions** subsystem (the
  primary case); memory is the explicit opt-in via `rl mem`.
- **Marks fold into their subsystem.** Bookkeeping verbs live under
  `rl session mark …` / `rl mem mark …`, not at the top level.
- **Global/registry plumbing** lives under `rl data <sub>`.

## Target command surface

**Flat (daily entry points / views):**

```
rl overview [<selector>] [flags]   # cross-cutting view (sessions + memory column)
rl triage   [flags]                # triage sessions (primary subsystem)
rl prune    [flags]                # prune tiny + stale sessions
```

**`rl session <sub>`** — per-session operations:

```
rl session refresh                 # reconcile registry from disk (was `rl sync scan`)
rl session set <args>              # record one session's classification (was `rl sync apply`)
rl session enrich                  # [reserved — Spec 2] LLM enrichment pass
rl session archive <sel>
rl session delete  <sel>
rl session complete <sel>          # [reserved — Spec 2] harvest + archive
rl session snooze  <sel>
rl session wake    <sel>
rl session name    [--project <sel>]
rl session relocate <args>
rl session mark <harvested|reviewed|cleaned> <args>
```

**`rl mem <sub>`** — memory subsystem:

```
rl mem refresh
rl mem prune [--delete] [--project <sel>]
rl mem triage [--project <sel>]
rl mem overview [<selector>]
rl mem mark <cleaned|reviewed> <args>
```

**`rl data <sub>`** — global / registry plumbing:

```
rl data export <args>
rl data tokens <args>
rl data migrate
```

**`rl config <sub>`** — **[reserved — Spec 2]** settings (LLM provider/key, harvest
target).

Top level is now **3 daily verbs + 4 namespaces** (`session`, `mem`, `data`,
`config`), down from 20 flat commands.

## Old → new mapping

| Old command | New command |
|---|---|
| `overview` | `overview` (unchanged; `--memory` removed → `mem overview`) |
| `triage` | `triage` (unchanged; `--memory` removed → `mem triage`) |
| `prune` | `prune` |
| `sync scan` | `session refresh` |
| `sync apply` | `session set` |
| `archive` | `session archive` |
| `delete` | `session delete` |
| `snooze` | `session snooze` |
| `wake` | `session wake` |
| `name` | `session name` |
| `relocate` | `session relocate` |
| `mark-harvested` | `session mark harvested` |
| `mark-reviewed` | `session mark reviewed` |
| `cleaned` | `session mark cleaned` |
| `mem-scan` | `mem refresh` |
| `mem-prune` | `mem prune` |
| `mem-cleaned` | `mem mark cleaned` |
| `mark-mem-reviewed` | `mem mark reviewed` |
| `overview --memory` | `mem overview` |
| `triage --memory` | `mem triage` |
| `export` | `data export` |
| `tokens` | `data tokens` |
| `migrate` | `data migrate` |
| — | `session enrich` *(reserved, Spec 2)* |
| — | `session complete` *(reserved, Spec 2)* |
| — | `config` *(reserved, Spec 2)* |

Every command keeps its existing flags and behavior; only its name/path changes.
The `--memory` flag on `overview`/`triage` is removed — that scope now lives in the
`rl mem` namespace.

## Backward compatibility

**Hard cut, no aliases.** This is a personal, pre-1.0 tool with a single consumer set
(the six skills) that is updated in lockstep with the rename. Keeping deprecated
aliases would preserve the clutter this spec exists to remove. Old names print the
usage/error and exit non-zero, same as any unknown command today.

## Dispatch mechanics

`cli.py` currently holds a flat `DISPATCH: dict[str, Callable[[list[str]], int]]`
(`commands.TABLE`). Introduce **one level of nesting**: a top-level token resolves to
either a leaf callable (flat verbs) or a **group table** (`session`, `mem`, `data`).

- `commands.TABLE` maps top-level tokens to `Callable | dict[str, Callable]`.
- `main()`: look up `argv[0]`. If it resolves to a callable, call it with `argv[1:]`
  (unchanged path). If it resolves to a dict, treat `argv[1]` as the sub-command,
  look it up in the group table, and call it with `argv[2:]`. Missing/unknown
  sub-command prints the group's usage and returns exit code 2.
- `mark` inside `session`/`mem` is a **second** nesting level
  (`session mark harvested`). Model each `mark` as its own small group table nested
  in the parent group. `main()` resolves at most three tokens: `group sub [mark-kind]`.
- Existing per-command `argparse` parsers inside each `cmd_*` function are untouched —
  they still see only their own residual args.

The command modules under `ringleader/commands/` keep their existing `cmd_*`
functions; only the wiring in `commands/__init__.py` (the table shape) and `cli.py`
(the nested lookup + usage rendering) changes.

## Help / usage output

`rl` (no args) and `rl --help` print:

- the three flat verbs with one-line descriptions, then
- the namespaces, each with its sub-commands listed.

`rl <group>` with no sub-command (e.g. `rl session`) prints that group's sub-commands
and returns 0. `rl <group> <bad>` prints the group usage to stderr and returns 2.
Reserved verbs (`session enrich`, `session complete`, `config`) are listed in help
with a `(coming soon)` marker and, if invoked, print "not yet implemented — see Spec
2" and return 2.

## Skill call-site updates

The six skills invoke `rl` by name; each call-site is updated to the new path. Known
call-sites (from the current skills):

- `session-overview`, `session-sync`: `rl overview*`, `rl sync scan`/`rl sync apply` → `rl session refresh`/`rl session set`
- `session-name`: `rl name`, `rl name --project` → `rl session name …`
- `session-triage`: `rl triage`, `rl prune`, `rl archive`, `rl delete`,
  `rl mark-harvested`, `rl mark-reviewed`, `rl cleaned`, `rl export` → new paths
- `session-harvest`: `rl mark-harvested`, `rl export` → `rl session mark harvested`,
  `rl data export`
- `memory-triage`: `rl triage --memory`, `rl mem-scan`, `rl mem-prune`,
  `rl mem-cleaned`, `rl mark-mem-reviewed`, `rl overview --memory` → `rl mem …`

Each skill's SKILL.md prose and examples are updated to match. A grep for
`rl [a-z]` across `.claude/skills/` after the change must show only new-surface paths.

## Testing

- **Dispatch routing:** table-driven test that each new path (`rl session archive`,
  `rl mem refresh`, `rl data export`, `rl session mark harvested`, …) routes to the same
  `cmd_*` function the old flat name used. Assert old flat names are now unknown
  (exit 2).
- **Group usage:** `rl session` / `rl mem` / `rl data` with no sub-command return 0
  and list sub-commands; `rl session bogus` returns 2.
- **Reserved verbs:** `rl session enrich` / `rl session complete` / `rl config`
  return 2 with the "not yet implemented" message.
- **Behavior preserved:** existing per-command tests keep passing against the new
  paths (update the argv they pass in). No behavioral assertions change.
- **Skill lint:** a test (or CI grep) that no old command name appears in
  `.claude/skills/`.

## Out of scope

- Any LLM behavior (`enrich`, `complete`, harvest, `config`, Model/Target seams) — Spec 2.
- The TUI — Spec 3.
- Changing what any command *does* — this is a pure rename/regroup.
