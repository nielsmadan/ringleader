# 0004: Organize the CLI around daily verbs and noun groups

## Context

The command surface grew past forty entry points. A flat namespace would have put
`rl mark-mem-reviewed` beside `rl overview` with no signal about which one a person runs daily
and which exists for a skill to call.

## Decision

Two tiers:

- **Flat daily verbs** — `overview`, `triage`, `prune`, `roundup`, `usage`, `inbox`,
  `harvests`, `doctor`, plus the setup verbs. These are what a person types.
- **Noun groups** — `rl session`, `rl mem`, `rl data`, `rl workspace`. Plumbing, mostly called
  by skills. `rl <group>` with no subcommand lists that group's verbs.

Dispatch is a nested dict (`commands.TABLE`), so a group is just a dict and the router recurses
without special cases.

## Consequences

- `rl --help` stays readable at 40+ commands because it lists twelve verbs and four groups.
- Moving a command between tiers is a one-line dict change.
- The tiering is a curation claim, not a technical one — it has to be revisited as verbs get
  promoted or demoted by actual use.

*Amended 2026-11-08:* a fifth group, `rl handoff`, holds `rl handoff triage`. It is not skill
plumbing: the circus app calls it (`--dry-run --json`) and people can too. It is a group so that
handing other work to an agent (publishing harvests, as the inbox already does) can join it
as `rl handoff <purpose>`. It is unrelated to `rl session handoff`, which forks a session into
another agent; the two share a word, not code.

See [tech/cli-and-commands.md](../tech/cli-and-commands.md).
