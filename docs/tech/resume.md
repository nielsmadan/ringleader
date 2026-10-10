# Resume

`resume.py`, `picker/app.py`, `commands/resume.py`: selecting a dormant session and handing the
process to an agent.

## Purpose

Three layers, split so the two interesting ones are testable without a terminal:

- **`resume.py`** — pure selection logic. Scope filtering, the fuzzy scorer, ranking, badges,
  and launch-directory resolution. No Textual, no disk, no subprocess.
- **`picker/app.py`** — the Textual TUI. Renders, filters, and **returns a `Choice`**. It never
  execs.
- **`commands/resume.py`** — argument parsing, validation, the handoff write, and the single
  `chdir` + `execvpe`.

The split exists because the process is replaced. Nothing may exec until Textual has released the
terminal, so the app returns a decision and the command carries it out.

## How it works

### Selection (`resume.py`)

`candidates(reg, scope=, cwd=)` filters `reg["threads"]` by `eligible` (a non-empty `sessionId` —
deliberately permissive) and `in_scope`, then sorts by `mtime` descending. `rank(threads, query)`
applies the scorer; a blank query is recency order.

`Scope` is `CWD` / `RECURSIVE` / `ALL`, compared as paths with directory-boundary awareness.
Deliberately **not** `projects.matches()`, which also matches git labels and roots — see
[sources.md](sources.md) for the same distinction in `slug.is_beneath`.

`score(query, text)` is a hand-rolled fzf-style subsequence scorer — no third-party dependency,
matching the reasoning in `namefilter.matches`. It scores **both** a left-to-right and a
right-to-left alignment and takes the better: a single-direction greedy scan is blind to a literal
run at the other end, which ranked an exact substring below a scattered match.

Four weighted ratios — consecutive run, word boundary, earlier first match, shorter text — sum to
at most `_MAX_SCORE`. That bound is load-bearing: `match()` adds `_TIER_BONUS = _MAX_SCORE + 1`
per tier, which is what guarantees a blurb hit can never outrank a name hit regardless of query
length. Tiers mirror `search.py`: name, then id, then blurb fields.

`badges(thread, now_iso)` reports `filtered`, `snoozed`, `kept`, `finishing`, `harvested`,
`abandoned`, `drifted`, in that order.

`launch_cwd(thread, threads)` answers "which directory must the agent start in". Usually the
recorded `project`. For a `claude-code` session where `scanner.stored_under(file, project, source)` is false
— the transcript is filed under a different directory's slug — it scans `threads` for an undrifted
sibling filed in the same place and uses that sibling's cwd. **Pass the full thread list**, or
recovery silently falls through to the drifted directory.

### The picker (`picker/app.py`)

`PickerApp` is `Header` / `Input#filter` / `ListView#rows` / `Label#empty` / `Footer`.

`on_mount` renders **synchronously from the registry**, then starts a `@work(thread=True)` worker
that runs `refresh.auto_refresh` and the presence probe and updates in place. The scan is skipped
when `lastScan` is under a minute old, because the command already scans for a query the registry
cannot answer and a second full scan behind the picker would double that cost. Query and
highlighted session survive the update. Every widget touch from that worker goes through
`call_from_thread`; the callback is synchronous because `call_from_thread` typed with a
`None`-returning coroutine solves to `Never` and fails mypy.

Presence comes from `PresenceClient.require_running_session_ids()`, matched on the
`(agent, session_id)` **pair** — presence names agents `claude` where the registry says
`claude-code`, so `agent_name()` maps between them, and an id colliding across two agents does not
produce a false badge. Any failure degrades to no badges and never fails the app.

`Choice(thread, target, threads, confirmed)` is the result; quitting exits `None`. `threads` is
the registry the picker converged on, and `confirmed` records that the user already answered the
running-session modal, so the command does not ask a second time. `action_choose` is
`@work(exclusive=True)`: with `--to` set no modal is pushed, so two fast `enter` presses would
otherwise both reach `self.exit`.

### The command (`commands/resume.py`)

Validates, then execs exactly once. Order matters — every check runs before any side effect,
because after `execvpe` there is no recovering:

1. resolve the target agent name (`claude` → `claude-code`)
2. resolve the session — `--last` takes the newest candidate in scope, an exact id or an
   unambiguous prefix of at least four characters skips the picker, and anything else pre-fills
   it. Both non-interactive paths earn one scan on a miss, then refuse; neither falls back to the
   picker
3. resolve and verify the launch directory
4. check the binary is on `PATH` — before the render, so a missing binary costs nothing
5. **same agent**: render the handoff transcript if any (there isn't one), ask if presence
   reports it already running, then `os.chdir(dir)` + `os.execvpe`.
   **`--to` a different agent**: try `_native()` first (below); only on refusal does it fall
   back to steps 6–7 below, after asking
6. render the Markdown handoff transcript
7. ask, if presence reports the session already running and the target is its own agent (never
   reached for a cross-agent handoff, which starts a new session)
8. write the transcript; `os.chdir(dir)`; `os.execvpe(argv[0], argv, environ)`

The running-session guard lives here rather than only in the picker, because `rl resume <id>` and
`rl resume <id> --to <its own agent>` never construct one. A probe that cannot answer
(`PresenceUnavailable`) degrades to not asking, never to "nothing is running".

### Native conversion (`_native()` in `commands/resume.py`)

`--to <agent>` without `--markdown` calls `portability.native_resume.native_resume()` — the same
`ConversionService` that backs `rl session convert`/`fork` — before ever considering the
Markdown fallback:

1. an already-converted, still-**active** target for this source+target pair is reopened
   directly, skipping the version gate below (reopening writes nothing, and requiring
   verification to reopen a session the agent already owns would block every resume after an
   agent update); an archived/deleted/missing one is forked fresh instead, and later resumes
   reopen that fork. Reopening needs the prior conversion to match the current source snapshot
   (so a source that gained turns converts anew), `PORTABLE_TRANSFORM_VERSION`, target store and
   workspace, writer profile and title, and to belong to the same `--registry`
2. otherwise, `inspect_target()` checks whether the installed target version is pinned or
2. otherwise, `inspect_target()` checks whether the installed target version is pinned or
   already self-verified on this machine. If not, and the session is interactive, it runs the
   agent's offline self-verification harness (`verify.workflow.verify_and_record`, the same
   workflow as `rl doctor --verify-conversion`) and records the result
   in `verified-targets.json` before retrying; non-interactively (scripted, `--dry-run`) it
   either reports `would-verify` or refuses
3. the conversion publishes through the normal journal (`operations/`), and the launch
   constructed from the fresh (or reopened) target replaces steps 6–7 above

Only `PreservationError` (including `NeedsVerification`) and I/O errors before publication are
refusals that offer the Markdown fallback; any other exception is a conversion failure. The
launch request sets a native root variable (`HOME`, `XDG_*`, `TMPDIR`, `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, `PI_CODING_AGENT_DIR`, `FACTORY_HOME_OVERRIDE`, `OPENCODE_DB`) only when the
recorded root differs from what the agent resolves from the inherited environment — Claude
names its keychain item after `CLAUDE_CONFIG_DIR` whenever it is set, so exporting an unset one
would log the user out.

`--dry-run --to <agent>` prints which of these branches would fire (`would-verify`,
`would-convert`, `reused`, `forked`, or `refused` with what the fallback would do) and launches
nothing, the same contract as the same-agent dry run.

Cross-agent continuation therefore has two layers: `_native()`'s native conversion, with the
`_fallback()` seeded Markdown handoff only reached on refusal or explicit `--markdown`.
[ADR 0013](../decisions/0013-native-conversion-behind-resume.md) records why conversion comes
first; [ADR 0011](../decisions/0011-seeded-handoff-over-native-conversion.md) still governs the
fallback itself.

`runner`, `picker`, `which`, `refresher` and `presence` are all injectable, so no test execs,
chdirs or prompts the test process.

## Key entry points

- `resume.candidates`, `resume.rank`, `resume.launch_cwd`, `resume.badges`
- `resume.agent_name` / `resume.source_name`, `resume.launchable_agents` — shared by the picker
  and the command
- `picker.app.PickerApp`, `picker.app.Choice`, `picker.app.row_text`
- `commands.resume.cmd_resume`
- `tui/keys.py:nav_bindings(priority=)` — the ctrl+j/ctrl+k motions every ringleader TUI carries

## Gotchas

- **`ctrl+a` and `ctrl+k` collide with the focused `Input`.** Textual binds `home,ctrl+a → home`
  and `ctrl+k → delete_right_all`. Without `priority=True` the user's "move up" key deletes the
  rest of their query. That is why `nav_bindings` takes a `priority` flag rather than being a
  constant, and why priority bindings are gated in `check_action` so a modal keeps its own keys.
- **Row text is `rich.text.Text`, not `str`.** Session names contain brackets —
  `rl config filter add '[cadet-city]*'` is the documented example — and a `Label` markup-parses a
  `str`, silently eating `[cadet-city]` and raising `MarkupError` on `[/]`.
- **The deferred re-render must check the app is alive.** The scan runs behind an interactive
  picker, so the user can quit or choose before it lands; an unguarded callback then renders
  against a torn-down screen.
- **`launch_cwd` needs the registry the picker converged on**, which only the picker can hand
  over. Textual's shutdown cancels the scan worker without joining its OS thread, so `App.run()`
  can return before the scan has persisted anything; reading the registry back off disk is a
  fallback for a `Choice` that carries no list, not the answer.
- **`--dry-run` writes nothing**, including the handoff transcript. It still renders, so render
  failures are still reported, and prints the path as `would write:`.
- **Nothing prunes `handoffs/`.** Unlike `archive_root()`, it has no retention clock. Files are
  written `0o600` and the directory is created `0o700`, because they are full conversations and
  its listing alone names which sessions were handed off and when.
- **Self-verification only runs interactively.** `--to <agent>` on an unverified target version
  calls out to `rl doctor`'s harness machinery (`allow_verify=_interactive()`); a non-interactive
  or `--dry-run` invocation reports `would-verify` instead of running it, so a script never blocks
  for a minute on a terminal it cannot write to.

## Why

The fuzzy filter is hand-rolled rather than a dependency because `textual` is the package's only
runtime dependency and `namefilter.py` already set that precedent deliberately.

Cross-agent continuation tries a native conversion first and only falls back to a *seeded
handoff* — the target reads a Markdown export and starts fresh — when conversion refuses or
`--markdown` asked for it directly. [ADR 0011](../decisions/0011-seeded-handoff-over-native-conversion.md)
records why the seeded handoff exists at all; [ADR 0013](../decisions/0013-native-conversion-behind-resume.md)
records why native conversion now comes first.

## Related

- [features/resuming-sessions.md](../features/resuming-sessions.md)
- [sources.md](sources.md) — `SessionLaunchingSource`, the `launches_sessions` capability
- [inbox.md](inbox.md) — the other Textual TUI, and the `check_action` precedent
- [registry.md](registry.md)
