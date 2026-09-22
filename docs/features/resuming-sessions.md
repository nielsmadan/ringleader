# Resuming sessions

`rl resume` — find a dormant session from any agent and re-enter it, in its own agent or another
one.

## What it does

A picker (Textual) over every session the registry knows, filtered as you type, sorted
most-recent-first. Choosing one replaces the `rl` process with the agent's own resume command,
started in the session's directory.

```sh
rl resume                  # picker, sessions recorded in this directory
rl resume 3f2a91c7         # unambiguous id prefix — no picker, straight in
rl resume auth             # picker, pre-filtered to "auth"
rl resume 3f2a91c7 --to codex   # hand it to a different agent
rl resume 3f2a91c7 --dry-run    # print the directory and argv, launch nothing
```

## Scopes

| Key | Flag | Shows |
|---|---|---|
| — | (default) | sessions recorded at exactly this directory |
| `ctrl+r` | `--recursive` | this directory and everything beneath it |
| `ctrl+a` | `--all` | every session on the machine |

Scope is a **path** comparison, directory-boundary aware — `/w/api` does not match `/w/apiary`.
It is not the git-project matching `rl overview <selector>` uses, because resuming is about the
directory the agent will start in, not about which repo the work belongs to.

## What appears

Every live session, whatever its triage state. Filtered, snoozed, kept, harvested, abandoned and
finishing sessions are all listed and **labelled**, not hidden — resuming is not triage, and
`rl session search` already sets the precedent that silently withholding a session you asked for
is worse than noise. Archived sessions are the exception: their transcripts have moved out of the
agent's reach, so they are absent, and asking for one by id says so and names
`rl session unarchive`.

Rows carry the agent, the age, the name, any badges, and a one-line preview. A session presence
reports as live is badged `running`; resuming it in its own agent asks for confirmation first,
because two agents appending to one transcript is not recoverable. A handoff to a different agent
does not ask, because it starts a new session and leaves the source untouched.

## Two ways to continue

**Same agent** — the default, and what the agent's own picker would have done:

| source | command |
|---|---|
| `claude-code` | `claude --resume <id>` |
| `codex` | `codex resume <id>` |
| `opencode` | `opencode --session <id>` |
| `pi` | `pi --session <id>` |

**A different agent** — a *seeded handoff*, not a conversion. The session is rendered to Markdown,
written under `~/.local/share/ringleader/handoffs/`, and the target agent is launched fresh with a
prompt pointing at that file. The target has read the old session; it is not continuing it. The
loss is real and deliberate — see [ADR 0011](../decisions/0011-seeded-handoff-over-native-conversion.md).

The source session is never modified, archived or marked by a handoff.

## Starting directory

Resuming always `chdir`s first. This is not a convenience: `claude --resume <id>` resolves only
within the current directory's project dir, and there is no global by-id lookup, so starting in
the wrong place means the session is simply not found.

The directory is usually the session's recorded cwd. For a Claude session whose transcript is
filed under a *different* directory's slug — a worktree, or a session that cd'd — the recorded cwd
is the wrong answer, and the launch directory is recovered from a sibling session filed in the
same place. Such a session is badged `drifted`.

## Freshness

The picker renders from `registry.json` immediately and scans behind it, updating in place. A scan
of a large store takes seconds and a picker that takes seconds to open is not worth using. A query
that the registry cannot answer at all triggers one scan and a retry before failing, so a session
created since the last roundup is still reachable.

## Related

- [views-and-triage.md](views-and-triage.md) — the read surfaces, and `rl inbox`, the other TUI
- [session-lifecycle.md](session-lifecycle.md) — the states the badges report
- [workspace-portability.md](workspace-portability.md) — why a recorded cwd drifts
- [ADR 0011](../decisions/0011-seeded-handoff-over-native-conversion.md) — seeded handoff vs. native conversion
- [`../tech/resume.md`](../tech/resume.md) — how it is built
