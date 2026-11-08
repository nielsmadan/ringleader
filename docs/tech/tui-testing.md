# TUI testing

`tui/snapshot.py`, `tui/types.py`, `devtools/tui_fixture.py`, `scripts/tui_shot.py`,
`tests/conftest.py` (`golden`), `tests/tui_helpers.py`, `tests/tui_e2e.py`: how the Textual apps
are tested and looked at without a person at a terminal.

## The tools

| Tool | Use it for |
|---|---|
| Pilot tests (`app.run_test(size=(100, 30))`) | Behaviour: keys, focus, what an action returns. |
| `golden(name, text, replace=...)` | Layout: the whole screen as text, compared with `tests/goldens/<name>.txt`. |
| `screen_text(app)` | The text a golden or a shot compares; modals render over their background. |
| `scripts/tui_shot.py` | Looking at a screen by hand, or showing one in a review. |
| `build_fixture(...)` | A deterministic synthetic world: registry, config, project dirs, optionally transcripts. |
| `tests/tui_e2e.py` | The real process in a real pty: argv parsing, terminal handoff, `exec`. |

`screen_text` copies Textual's `App.export_screenshot` but exports text instead of SVG. It reaches
into the compositor (`app.screen._compositor`), so it is test- and script-only: a Textual upgrade
that breaks it fails the suite, never a user.

## Goldens

```sh
uv run python -m pytest tests/test_tui_tooling.py --update-goldens   # rewrite, then read the diff
```

`--update-goldens` writes the file (creating directories) and passes; without it a missing golden
fails with the exact command to create it, and a mismatch fails with a unified diff. Review the
rewritten files with `git diff tests/goldens` before keeping them.

A golden is only stable when nothing in it depends on the machine or the moment:

- **The clock is pinned.** Every app takes `now: Clock` (`tui/types.py`) and renders ages from it,
  so `3d ago` stays `3d ago` next week. Never let an app reach for `datetime.now()` in a golden.
- **Paths are replaced.** Pass `replace={str(tmp_path): "<root>", str(Path.home()): "~"}`; the
  longest key is applied first. Lines are right-stripped and trailing blank lines dropped, the same
  normalization `screen_text` applies.
- **Nothing scans.** Inject a refresher that reloads the file and `presence=frozenset`.

## Shots

```sh
uv run python scripts/tui_shot.py --app picker --keys "down,down"
uv run python scripts/tui_shot.py --fixture 200 --size 120x40 --keys "settle,enter" --each
uv run python scripts/tui_shot.py --registry ~/.local/share/ringleader/registry.json
```

`--keys` takes comma-separated Textual key names; `settle` is the one pseudo-key, and runs the
app's settle step (the picker's waits for every worker, so do not `settle` while a modal opened by
a `push_screen_wait` worker is up — it never completes). `--each` prints a shot after every key,
otherwise one final shot is printed. A key that exits the app ends the run with `exited: <value>`.

The default is `--fixture 40 --now 2026-10-15T12:00:00+00:00`, built in a temporary directory
whose store and XDG variables are applied to the process first. `--registry PATH` copies the file
into a temporary directory, points `XDG_DATA_HOME`/`XDG_STATE_HOME` there, and runs the app
read-only, so a shot of live data never writes the live registry. It uses the real time unless
`--now` is given.

## The fixture

`build_fixture(root, threads=, now=, seed=, transcripts=)` lays out `root/work/proj-NN` project
directories (one per 14 threads), a registry at `root/xdg-data/ringleader/registry.json` and a
`config.json` that filters `zz-hidden*` names. The lifecycle mix mirrors live data: a few
classified, kept, snoozed (waking soon and one expired), one harvested, one marked
finish-on-exit, filtered names, and archived/deleted tombstones. `fixture.env` holds the six
store and XDG variables that make the world self-contained.

`transcripts=False` writes only the registry and is what goldens use. `transcripts=True` writes
Claude Code transcripts and builds the registry from a real scan, so a later scan changes nothing:
use it when the test runs a scan.

## End to end

`run_scenario` spawns `python -P -m ringleader <argv>` under `pexpect` and feeds the output through
`pyte`, so `Expect("text")` matches what is on the screen, not raw escape codes. It runs
`sys.executable -m ringleader` rather than an installed `rl`, so it tests this checkout.
`isolated_env` strips the agent session probes and terminal size variables, points `HOME`, the
stores and XDG at the test's directory, and sets `PATH` to the fake-agent directory, the
interpreter's directory and `/usr/bin:/bin`: no real agent CLI is reachable, so an `exec` lands
in a `make_fake_agents` script that records its directory and argv.

## Waiting

Wait for background results with `tui_helpers.until(pilot, predicate)`, which polls with
`pilot.pause`. `app.workers.wait_for_complete()` only sees Textual workers: work on a daemon
`threading.Thread` is invisible to it, so it can return before the result lands.
