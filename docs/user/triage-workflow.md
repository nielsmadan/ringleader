# The triage workflow

The recurring loop: look at what's dormant, decide something about each one, and park what
you're not deciding yet.

## The loop

```sh
rl overview          # where is my work spread
rl triage            # what awaits a decision
rl inbox             # work the queue interactively
```

`rl roundup` refreshes, prunes and enriches in one shot. `rl inbox` offers to run it for you
when the registry has gone stale (older than `roundup_stale_hours`, default 8).

> `rl roundup` **deletes** — it runs prune with deletion enabled. Bare `rl prune` only previews.

## Five decisions

For each dormant session, exactly one of:

| Decision | Command | When |
|---|---|---|
| **Finish it** | `rl session harvest <id>` then `rl session finish <id>` | done; keep what it taught |
| **Keep going** | leave it, or name it | still live work |
| **Snooze** | `rl session snooze <id> --for 14` | not this week |
| **Keep long-term** | `rl session keep <id>` | never triage this again |
| **Delete** | `rl session delete <id>` | nothing worth keeping |

### Snooze vs. keep

Both hide a session, and they are not interchangeable:

- **Snooze** is temporary. The session comes back on `rl session wake`, or automatically with
  `--until <iso>` / `--for <days>`. Use it for "ask me again later".
- **Keep** is permanent. The session leaves the triage inbox **for good, even when it sees new
  activity**. Use it for a long-running thread you never want to see in triage. `rl overview
  --kept` lists them; `rl session unkeep` reverses it.

## Narrowing the queue

The full backlog is discouraging, and you don't have to face it at once:

```sh
rl triage --recent 7              # only the last week
rl triage --project ringleader    # one repo
rl triage -u                      # only never-classified sessions
rl overview --status blocked      # what's waiting on you specifically
```

`rl session mark cleaned <selector>` records a project as just-triaged, so the overview can show
what's done versus what needs another look. The `ringleader-cleanup` skill uses this to work the
whole backlog in resumable chunks across several sittings.

## Selectors

Anywhere a `<selector>` is accepted, it's a **git project label** (`ringleader`) or a **cwd
path** — never a session id. Worktrees, clones and monorepo subdirs of one repo collapse into a
single project.

```sh
rl overview ringleader
rl overview ~/wrksp/ringleader/dev2
rl triage --project ringleader --project juggler    # repeatable
```

## Classifying without doing it by hand

`rl session enrich` classifies pending sessions (status, what-it-is, a suggested name) using a
configured model:

```sh
rl config show                 # what engine would be used
rl config set ai claude        # pin one
rl session enrich --limit 20
```

With no engine available this is a clean no-op, and classifying stays a manual step inside the
`ringleader-triage` skill. See [Agent setup](agent-setup.md#the-ai-engine).

## Letting an agent drive

The skills exist to run this loop for you:

- `ringleader-overview` — the morning read.
- `ringleader-triage` — the recurring pass over the last few days.
- `ringleader-cleanup` — the same over the entire backlog, resumable.

## Next

- [Finishing a session](finishing-a-session.md)
- [Archives and retention](archives-and-retention.md)
