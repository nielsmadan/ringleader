# Getting started

## Install

```sh
just install-local        # migrates any legacy repo-root state, then `uv tool install .`
```

or directly, `uv tool install .`. Either way this puts `rl` on `PATH` as a real console command —
run it as `rl <command>` from anywhere, not `./rl`.

To run from a source checkout without installing, prefix every command with `uv run`:

```sh
uv run rl overview
```

**Working on ringleader itself?** Use `just install-editable`. A normal install copies the
source, so the global `rl` silently falls behind your checkout until you reinstall; an editable
one tracks the working tree, so `rl` is always what you last edited — and `rl install skills`
resolves to the repo's own `.agents/skills/`.

All three install recipes finish with `rl update all --yes`, so already-wired skills stay in sync
with the repo. If you haven't wired anything, it's a no-op — `update` never opts an agent in on
its own.

## Where state lives

The registry, the archive, and the pending harvest queue live under
`~/.local/share/ringleader/` (or `$XDG_DATA_HOME/ringleader` if set) — **not** in the repo.
`just install-local` runs `rl data migrate` first to move any pre-existing repo-root
`registry.json` / `archive/` there.

That means several checkouts of ringleader share one registry, which is what you want: there is
one machine's worth of sessions.

## First look

```sh
rl overview
```

This scans the local Claude Code, Codex and OpenCode stores that are present, builds the registry,
and prints a rollup by git project. The first run is the slow one — it parses every session.
Later runs only parse what changed.

```sh
rl overview --sessions        # flat list instead of the rollup
rl overview ringleader        # deep-dive one project
rl triage                     # just what awaits a decision
```

## First triage pass

Don't try to clear the whole backlog on day one. Bound it:

```sh
rl triage --recent 7
```

Then work the queue interactively:

```sh
rl inbox
```

`j`/`k` to move, `h` harvest, `d` delete, `u` undo, `s` snooze, `K` keep long-term, `;` detail,
`q` quit. `tab` cycles the inbox / selected / harvesting views.

If you'd rather have an agent drive it, wire up the skills ([Agent setup](agent-setup.md)) and
invoke `ringleader-init`, which verifies the install, checks your retention settings, and runs a
bounded first pass.

## Check retention before you rely on any of this

```sh
rl doctor
```

Among other things this reports Claude Code's `cleanupPeriodDays` — the GC clock that deletes
transcripts out from under you. Knowing that number tells you how much time you actually have to
triage a session before the decision is made for you.

## Next

- [The triage workflow](triage-workflow.md) — the recurring loop.
- [Agent setup](agent-setup.md) — the skills that drive all of this.
