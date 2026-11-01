# Resuming sessions

`rl resume` — find a dormant session from any agent and re-enter it, in its own agent or another
one.

## What it does

A picker (Textual) over every session the registry knows, filtered as you type, sorted
most-recent-first. After choosing a session, choose whether to resume it, fork a new session
in the same agent, or hand it to another agent. The selected command replaces the `rl` process
and starts in the session's directory.

```sh
rl resume                  # picker, sessions recorded in this directory
rl resume --last           # the most recent session here, no picker
rl resume 3f2a91c7         # unambiguous id prefix — no picker, straight in
rl resume codex:<sessionId>    # qualified id — exact match, any directory, no picker
rl resume auth             # picker, pre-filtered to "auth"
rl resume 3f2a91c7 --to codex   # hand it to a different agent: native conversion, consented fallback
rl resume 3f2a91c7 --to codex --markdown   # skip native conversion, go straight to the Markdown handoff
rl resume 3f2a91c7 --dry-run    # print the directory and argv, launch nothing
rl resume 3f2a91c7 --worktree   # fork into a separate checkout with its own agent
```

A qualified id, `<source>:<sessionId>` with the full session id (`claude` works for
`claude-code`), names one session exactly, so it ignores the scope flags and skips the picker.
One that matches nothing earns one scan, then refuses without suggesting `--recursive` or
`--all`, since widening the scope cannot help.

## The most recent one

`rl resume --last` takes the newest session in scope and re-enters it, with no picker and no
prompt. It obeys the scope flags, so `--last --recursive` is the newest anywhere beneath this
directory and `--last --all` the newest on the machine.

Each agent already has its own version of this — `claude -c`, `codex resume --last`,
`droid resume --last`, `opencode -c`, `pi -c`. What none of them can do is cross the agent
boundary: if the last thing you touched here was a Codex session, `claude -c` cannot reach it.
`--last` re-enters whichever agent the session actually belonged to, which is the only reason it
earns its place.

It rescans its scope first, so a session you only just quit counts even if nothing has scanned
since. It refuses rather than falling back to the picker — `--last` asked not to be asked. An
empty scope earns one full scan before it gives up, and `--last` cannot be combined with a query.

## Scopes

| Key | Flag | Shows |
|---|---|---|
| — | (default) | sessions recorded at exactly this directory |
| `ctrl+t` | `--recursive` | this directory and everything beneath it |
| `ctrl+a` | `--all` | every session on the machine |

Scope is a **path** comparison, directory-boundary aware — `/w/api` does not match `/w/apiary`.
It is not the git-project matching `rl overview <selector>` uses, because resuming is about the
directory the agent will start in, not about which repo the work belongs to.

## Refreshing the picker

The picker shows cached sessions immediately, then refreshes this workspace before scanning
all sources. Its status line shows when workspace results are ready and when the full scan
finishes. Sources without scoped discovery join in the full scan.

Press `ctrl+r` or `F5` to discover new sessions without reopening the picker. Repeated presses
during a refresh queue one more pass. The filter, scope and highlighted session are preserved.
A failed scan keeps the last displayed results and can be retried with the same keys. There is
no periodic refresh. In the inbox, Resume uses the inbox scan service.

## What appears

Every live session, whatever its triage state. Filtered, snoozed, kept, harvested, abandoned and
finishing sessions are all listed and **labelled**, not hidden — resuming is not triage, and
`rl session search` already sets the precedent that silently withholding a session you asked for
is worse than noise. Archived sessions are the exception: their transcripts have moved out of the
agent's reach, so they are absent, and asking for one by id says so and names
`rl session unarchive`.

Rows carry the agent, the age, the name, any badges, and a one-line preview. A session presence
reports as live is badged `running`; resuming it in its own agent asks for confirmation first,
because two agents appending to one transcript is not recoverable. Forking needs no reopening
confirmation: it starts a separate session. Cross-agent native conversion checks whether the
source or a reused target is running before proceeding.

## Ways to continue

**Same agent** — the default, and what the agent's own picker would have done:

| source | command |
|---|---|
| `claude-code` | `claude --resume <id>` |
| `codex` | `codex resume <id>` |
| `droid` | `droid resume <id>` |
| `opencode` | `opencode --session <id>` |
| `pi` | `pi --session <id>` |

**Fork in the same agent** — the second entry creates a fresh session continuing from the
source's current conversation state. The original is preserved, and the workspace stays the
same. Each selection forks anew. This uses the agent's native command:

| source | command |
|---|---|
| `claude-code` | `claude --resume <id> --fork-session` |
| `codex` | `codex fork <id>` |
| `droid` | `droid resume <id> --fork` |
| `opencode` | `opencode --session <id> --fork` |
| `pi` | `pi --fork <id>` |

Pi's CLI fork copies the session tree into a fresh file; its interactive `/fork` instead asks
for an earlier prompt. Native metadata follows each agent's rules. Forking creates no git
worktree or filesystem snapshot, and a running source can advance while the agent reads it.
The new session enters ringleader on the next scan; this path does not create conversion-journal
lineage. `rl session fork` remains the separate intermediate-format, journaled operation.

Use the picker to fork (`rl resume`, optionally with a text filter). An exact ID, `--last`, or
`--to` bypasses the action menu. `rl resume --dry-run` lets you select a fork and prints its
command without launching it.

**Fork into a worktree** — the third own-agent entry, or `--worktree` with a query or
`--last`. This always forks the conversation in its original agent and requires a Git
checkout with a commit. `--to` and `--markdown` cannot be combined with the flag.

| Agent | Creation and lifecycle |
|---|---|
| Claude Code | Native fork plus `--worktree`; Claude owns its hooks, naming, isolation, and cleanup |
| Codex | `codex fork <id> --worktree`; Codex owns the managed checkout |
| Droid | `droid resume <id> --fork --worktree`; Droid owns the checkout |
| OpenCode v2 | Native API creates the worktree, forks history, and moves the fork; rl verifies the directory before launch |
| Pi | rl creates a persistent branch/checkout at committed `HEAD`, then invokes `pi --fork <absolute-transcript>` there |

Names, destinations, base revisions, and setup follow the native harness settings. There is
no worktree-name argument. Pi's fallback uses a generated `rl-…` branch and a sibling
`<main-checkout-name>.worktrees/rl-…` directory. It launches at that checkout's root, copies
no uncommitted or ignored files, runs no setup script, and leaves cleanup to the user.
Submodules and separate Git directories use their working checkout, never their administrative
storage. For linked checkouts where Git cannot identify a valid main working checkout, the
destination is beside the current checkout instead.

Dry-run reports the owner and intended commands without creating a checkout, session, or
API request. Unsupported harness versions refuse before creation. Preparation errors report
any known retained resources; rl never automatically removes them. Repeating the action
requests another fork and follows the harness's own checkout reuse policy.

OpenCode's API returns the new identity before launch, so rl caches it for immediate resume.
Its v2 storage is not understood by the existing v1 scanner: cached rows remain available
while that source reports unavailable, but discovery and transcript-based lifecycle operations
for v2 need separate source support. Other harness forks enter the registry on the next scan.
Use `--all` to find a session whose new directory is outside the original picker scope.

See [agent worktrees](../reference/agent-worktrees.md) for the verified CLI surfaces and
upstream lifecycle differences. CLI delegation does not promise desktop-only features.

**A different agent** — `--to <agent>` tries a **native conversion** first: a fresh session in
the target agent, built from the source's visible history and active model context, that
genuinely continues it (the same mechanism as `rl session convert`/`fork`). A previously
converted target that's still active is reopened, as long as the source has not moved on
since; one that was archived or deleted is forked again, and later resumes reopen that fork. If the target agent's installed version has never been verified on this machine, the
first use self-verifies it offline (`rl doctor --verify-conversion`), which takes about a minute
and only happens interactively.

Native conversion can refuse — an unsupported native shape, a missing or incompatible native
store. On refusal, `rl resume` asks before falling back to the original *seeded handoff*: the
session rendered to Markdown, written under `~/.local/share/ringleader/handoffs/`, and the
target agent launched fresh with a prompt pointing at that file. Those exports stay until
`archive.retention_days` (default 0, keep forever) ages them out. The target has read the old
session; it is not continuing it. `--markdown` skips the native attempt and goes straight to
this fallback. See [ADR 0011](../decisions/0011-seeded-handoff-over-native-conversion.md) for why
the fallback exists, and [ADR 0013](../decisions/0013-native-conversion-behind-resume.md) for why
native conversion now comes first.

The source session is never modified, archived, or marked, either way.

## Starting directory

Resuming always `chdir`s first. This is not a convenience: `claude --resume <id>` resolves only
within the current directory's project dir, and there is no global by-id lookup, so starting in
the wrong place means the session is simply not found.

The directory is usually the session's recorded cwd. For a Claude Code or Droid session whose
transcript is filed under a *different* directory's slug — a worktree, or a session that cd'd —
the recorded cwd is the wrong answer, and the launch directory is recovered from a sibling
session filed in the same place. Such a session is badged `drifted`. Both agents key storage by
the launch directory's slug, so both need the recovery.

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
