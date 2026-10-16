# CLI and commands

How an `rl` invocation gets from `argv` to a handler: the nested dispatch table (`cli.py`), the
command registry (`commands/__init__.py`), and the one-module-per-verb convention.

For the *user-facing* contract of each command — flags, defaults, worked examples — see
[user/cli.md](../user/cli.md) and `README.md`. This doc covers the internals.

## Purpose

`cli.py` is the entry point and the whole router; it is under 80 lines and holds no argument
parsing of its own. Each leaf command owns its own `argparse.ArgumentParser`, which is why
`rl <anything> --help` works without the router knowing anything about flags.

## How it works

### The dispatch table

`commands.TABLE` is a **nested dict**: a string maps to either a callable (a leaf command) or
another dict (a group). There is no registration decorator and no plugin scan — the table is
written out literally, so the whole surface is greppable in one file.

```
TABLE = {
    "overview": overview.cmd_overview,      # leaf
    ...
    "session": _SESSION,                    # group (a dict)
    "mem": _MEM,
    "data": _DATA,
    "workspace": _WORKSPACE,
}
```

Groups nest arbitrarily — `_SESSION["mark"]` is itself a dict, giving
`rl session mark harvested`.

### `main()` flow

1. No args and an interactive terminal (stdin and stdout are TTYs, `TERM` is not `dumb`, and
   `session_env.current_session()` is `None`, via `_opens_inbox`) → rewrite `argv` to `["inbox"]`
   and dispatch normally. Otherwise no args, or `-h` / `--help` / `help` → print the two-tier
   usage and exit 0. An agent harness running bare `rl` in a pty gets usage, never a blocking TUI.
2. Walk `argv` token by token against the table, accumulating `crumbs` for error messages.
3. An unknown token prints `rl: unknown command '<tok>' in 'rl <crumbs>'` and the usage, exit 2.
4. A **callable** entry gets the remaining argv and its return value becomes the exit code
   (`entry(rest) or 0`, so a handler returning `None` exits 0).
5. Running out of tokens on a **dict** prints that group's subcommands and exits 0 — this is
   what makes bare `rl session` a listing rather than an error.

### Usage rendering

`_usage()` also prints one line saying that bare `rl` in a terminal opens the inbox.

`_FLAT` and `_GROUPS` in `cli.py` control the ordering of the top-level help. They are separate
from `TABLE`, so **a command added to the table but not to `_FLAT` dispatches correctly but is
invisible in `rl --help`.** `_RESERVED` marks names as `(coming soon)` and is currently empty.

### Command modules

One module per verb under `ringleader/commands/`, each exporting `cmd_<name>(argv) -> int`. The
convention throughout:

- Build a local `ArgumentParser` with `prog="rl <full command>"` so help text reads correctly at
  any depth.
- Take `--registry` wherever state is touched, defaulting to `paths.default_registry()`. Tests
  pass a temp path.
- Return an int exit code; print user-facing output to stdout and diagnostics to stderr.

Two handlers do their own sub-dispatch instead of nesting in `TABLE`: `usage` (`session`,
`windows`, `export`) and `config` (`show`, `set`). Both predate the group convention and
hand-roll the check on `argv[0]`.

## Key entry points

- `cli.main()` — the router.
- `commands.TABLE` — the whole surface, one literal.
- `commands/__init__.py` `_SESSION` / `_MEM` / `_DATA` / `_WORKSPACE` — the groups.
- `cli._FLAT` / `cli._GROUPS` — help ordering.

## Gotchas

- **`rl config --help` fails.** `config` hand-rolls dispatch and treats `--help` as an unknown
  subcommand, printing `unknown subcommand '--help'` and exiting 2. Same shape in `rl session
  mark --help`. The nested-group path handles this correctly; these two do not.
- **`rl data tokens` is not `rl usage`.** It calls `cmd_usage` with `--source claude` prepended,
  so it reports Claude Code only while accepting every other usage flag.
- **A leaf's return of `None` means success**, via `entry(rest) or 0`. A handler that wants a
  nonzero exit must return the int explicitly.
- **Adding a command needs two edits** — `TABLE` and, for a top-level verb, `_FLAT`.

## Why

A dict router rather than `argparse` subparsers: subparsers would centralize every flag in one
builder, making each command's interface a shared-file edit and slowing startup by constructing
the entire parser tree for every invocation. Per-command parsers keep each verb's interface local
to its module and build only the one parser that will actually run.

## Related

- [0004: daily verbs and noun groups](../decisions/0004-organize-the-cli-around-daily-verbs-and-noun-groups.md)
- [user/cli.md](../user/cli.md) — the full command reference
