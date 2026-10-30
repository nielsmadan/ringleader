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
deliberately permissive) and `in_scope`, then sorts by `mtime` descending. `rank(threads, query,
haystack_of)` keeps the matches, best tier first and most recent first within equal scores; a blank
query is recency order. `haystack_of` maps a thread to its `Haystack` — the cleaned, lowercased text
`match()` reads, which `Haystack.of` lowercases and the constructor refuses to accept unlowered — and defaults to building one per call; the picker passes a cache instead.

`Scope` is `CWD` / `RECURSIVE` / `ALL`, compared as paths with directory-boundary awareness.
Deliberately **not** `projects.matches()`, which also matches git labels and roots — see
[sources.md](sources.md) for the same distinction in `slug.is_beneath`.

`score(query, text)` is a hand-rolled fzf-style subsequence scorer — no third-party dependency,
matching the reasoning in `namefilter.matches`. It scores **both** a left-to-right and a
right-to-left alignment and takes the better: a single-direction greedy scan is blind to a literal
run at the other end, which ranked an exact substring below a scattered match.

Four weighted ratios — consecutive run, word boundary, earlier first match, shorter text — sum to
at most `_MAX_SCORE`. That bound is load-bearing: `match()` adds `_TIER_BONUS = _MAX_SCORE + 1`
per tier, which is what guarantees a lower tier can never outrank a higher one regardless of query
length. From best to worst:

1. the name (title, suggested name, else AI title) equals the query;
2. the name starts with it;
3. every whitespace-separated query word is a substring of the name;
4. fuzzy: the query, spaces removed, is a subsequence of the name or of the project label
   (`gitProjectLabel`, else the project directory's basename), or a prefix of the session id;
5. blurb: every query word is a substring somewhere in the name, project label, AI title,
   `whatItIs`, `summary` or `lastPrompt`.

Subsequence matching stops at the short identifying fields on purpose. Run over a several-hundred
character summary, almost any three letters appear in order — on a real 1,935-session registry
`gvw` matched 731 sessions — so blurbs match by substring only. This deliberately reverses the earlier rule of "subsequence over
every field, permissive by design" in `docs/plans/2026-09-21-rl-resume-ledger.md`; do not restore it.

The picker's ranking is deliberately not `rl session search`'s (`search.py`: name, id, blurb tiers,
literal substring or glob). The picker is an interactive fuzzy filter that re-ranks on every
keystroke and tolerates typos and abbreviations; search is a scripted lookup that must return
only literal hits. Sharing a matcher would make one of them worse, so they stay separate.

`badges(thread, now_iso)` reports `filtered`, `snoozed`, `kept`, `finishing`, `harvested`,
`abandoned`, `drifted`, in that order.

`launch_cwd(thread, threads)` answers "which directory must the agent start in". Usually the
recorded `project`. For a session whose agent files transcripts by launch directory, where
`drifted()` finds the transcript under a different directory's slug, it scans `threads` for an undrifted
sibling filed in the same place and uses that sibling's cwd. **Pass the full thread list**, or
recovery silently falls through to the drifted directory.

### The picker (`picker/app.py`)

The picker is a `PickerScreen`: `Header` / `Input#filter` / `Label#refresh-status` /
`Label#columns` / `OptionList#rows` / `Label#empty` /
`Footer`. `PickerApp` is a thin shell that pushes one on mount and exits with whatever it
dismisses, so `rl resume` behaves as before; a host app can push the same screen, where `escape`
dismisses `None` back to the screen below instead of quitting. The screen touches `self.app` only
to notify, push modals and check that the app is running. Its row renderer is `_render_rows`, not
`_render` (every `Widget` already has one). App-level queries resolve against the default screen,
so tests reach the picker through the `PickerScreen` on `app.screen_stack`.
The picker labels separate Last active (`mtime`, the source modification time) and Started
(`startedAt`, the first recorded timestamp or source creation timestamp) columns. Unknown starts
show `?`. A persistent refresh status distinguishes cached results, workspace results while the
full scan runs, queued refreshes, completion, unavailable sources, and failure.
Partial refreshes name the unavailable sources whose cached rows remain. Hosted pickers keep
showing refresh progress until the inbox scan finishes.
Source parser versions invalidate older caches once to populate start times.
For Claude Code, Droid and Pi, Started follows the first valid recorded timestamp, including
inherited history in copied transcripts. It does not measure when a fork was made.
The picker's structured `full_refresher` returns `Refreshed` on completion and raises on
failure; unavailable sources travel with the completed result.

The result list holds Rich text options, avoiding a widget per match. It keeps every result,
clips long rows with an ellipsis, and preserves the highlighted `(source, sessionId)` across
filtering, scope changes and refreshes. Filtering is debounced: each input change restarts a
`FILTER_DELAY` (120 ms) timer and only the last one renders, while `enter` and the scope keys
apply the input's current text first, so a choice never lands on the previous query's rows. A
render reuses three things: each thread's `Haystack` and each row's `Option` (its rendered text, per wall-clock minute) until a scan returns different threads (an identical rescan keeps them), held in one `_RowCache`, and one instance
of each slug-keyed source from `resume.slug_sources()` for the whole drift check. When the ranked
`(source, sessionId)` list, presence and the wall-clock minute are all unchanged, the option list is left
alone. `scripts/benchmark_picker.py` measures result replacement on a fixed workload.

The screen's `on_mount` renders **synchronously from the registry**, then starts an async Textual worker
which receives staged results through a queue from a daemon thread. Standalone `PickerApp`
runs `workspace_refresh()` first, publishes its result, then runs the full scan. Recursive
scope includes descendants; everywhere scope still prioritizes the launch directory. The full
result is published before the presence probe, so badge lookup cannot hold back new sessions.
With `--dry-run`, both stages update the displayed results without saving the registry. The
picker carries the last scan snapshot between stages and cycles, preserving unvisited rows
and reusing parsed sessions even when the disk registry remains unchanged.
It always scans on open, including when `lastScan` is recent. Only the async
worker touches widgets; cancelling it on exit drops late results and signals the thread to
skip any presence probe it has not started. The scan can outlive the UI, but cannot hold up
resume or process exit. Using Textual's `@work(thread=True)` here would put it in asyncio's
default executor, whose shutdown waits for running threads even after worker cancellation.

`ctrl+r` and `F5` request another refresh; `ctrl+t` widens to this tree and `ctrl+a` to
everywhere. Refreshes run serially and repeated requests during one cycle coalesce into one
rerun. The hosted picker forwards requests immediately to the inbox scan service, so a queued
scan survives leaving Resume. It polls the service's current attempt outcome on the UI thread;
the last usable snapshot remains separate from a failed attempt. It applies the current
generation and decision overlay when displaying the result. Failure keeps the latest
displayed rows; a failed workspace pass still proceeds to the full scan.
The inbox owns scan notifications, so its picker does not repeat the scan's delta note.

Presence comes from `PresenceClient.require_running_session_ids()`, matched on the
`(agent, session_id)` **pair** — presence names agents `claude` where the registry says
`claude-code`, so `agent_name()` maps between them, and an id colliding across two agents does not
produce a false badge. Any failure degrades to no badges and never fails the app.

`Choice(thread, target, threads, confirmed, action)` is the result; quitting exits `None`. `threads` is
the registry the picker converged on, and `confirmed` records that the user already answered the
running-session modal, so the command does not ask a second time. `action` is `resume` (default)
or `fork`. `AgentModal` returns an `AgentChoice(target, action)`, with resume and fork entries
for the original agent followed by other-agent handoffs. The modal half of choosing lives in
`ask_resume_choice(screen, thread, *, target, running, threads)` — a preset target skips the agent
modal, a same-agent resume of a running session asks `ConfirmModal` — so any screen can reuse it
from an async `@work` worker (it awaits `push_screen_wait`). `action_choose` is
`@work(exclusive=True)`: with `--to` set no modal is pushed, so two fast `enter` presses would
otherwise both dismiss the screen.

`AgentChoice`, `agent_row_text`, `AgentModal` and `ConfirmModal` live in `tui/modals.py` (and are
re-exported from `picker/app.py`) beside the other shared modals: `ChoiceModal[T]` (enter, digits
`1`–`9`, escape), `SnoozeModal(now)` (a `fuzzytime.parse_duration` input with an inline error) and
`AgentPickModal(title, agents)` (canonical source names in and out). Each binds its own keys,
because app bindings do not fire under a `ModalScreen`. `tui/rows.py:set_rows(option_list, rows,
keep=)` rebuilds an `OptionList` keyed by option id and restores the highlight — the kept key,
else the previous index clamped — since `set_options` resets it.

### The command (`commands/resume.py`)

`--last` prints flushed status lines to stderr before loading the registry, refreshing,
checking live sessions, and launching the agent. Refresh completion includes elapsed time;
the launch line includes total ringleader preparation time. A missing scan baseline or an
empty scope announces the full scan. Dry runs show refresh progress but skip presence and launch.

Validates, then execs exactly once. Order matters — every check runs before any side effect,
because after `execvpe` there is no recovering:

1. resolve the target agent name (`claude` → `claude-code`)
2. resolve the session — `--last` takes the newest candidate in scope, an exact id (bare, or
   qualified as `<source>:<sessionId>`) or an unambiguous prefix of at least four characters
   skips the picker, and anything else pre-fills
   it. Both non-interactive paths earn one scan on a miss, then refuse; neither falls back to the
   picker. `--last` also rescans its scope *before* choosing, through `refresh.scoped_refresh`
   (see [scanning and refresh](scanning-and-refresh.md#refresh)): the session just left is the
   likeliest answer and the one a registry scanned earlier lacks, while a full scan costs ~20s on
   a large machine. The baseline is each source's entry in `sourcesScannedAt`, not `lastScan`,
   because memory scans advance `lastScan` without looking at a session. An empty scope earns
   a full scan only if the rescan was not already one
3. resolve and verify the launch directory
4. check the binary is on `PATH` — before the render, so a missing binary costs nothing
5. **same agent**: select the source’s `resume_argv` or `fork_argv`. Resuming asks if presence
   reports it already running; forking creates a separate session without that confirmation.
   Then `os.chdir(dir)` + `os.execvpe`.
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

`runner`, `picker`, `which`, `refresher`, `scoper` and `presence` are all injectable, so no test execs,
chdirs or prompts the test process.

## Key entry points

- `resume.candidates`, `resume.rank`, `resume.launch_cwd`, `resume.badges`
- `resume.agent_name` / `resume.source_name`, `resume.launchable_agents` — shared by the picker
  and the command
- `picker.app.PickerScreen`, `picker.app.PickerApp`, `picker.app.Choice`, `picker.app.row_text`,
  `picker.app.ask_resume_choice`
- `tui/modals.py`, `tui/rows.py:set_rows`
- `commands.resume.cmd_resume`
- `tui/keys.py:nav_bindings(priority=)` — the ctrl+j/ctrl+k motions every ringleader TUI carries

## Gotchas

- **`ctrl+a` and `ctrl+k` collide with the focused `Input`.** Textual binds `home,ctrl+a → home`
  and `ctrl+k → delete_right_all`. Without `priority=True` the user's "move up" key deletes the
  rest of their query. That is why `nav_bindings` takes a `priority` flag rather than being a
  constant. The picker's priority bindings sit on the `PickerScreen`, so a modal pushed above it
  takes them out of the binding chain and keeps its own keys.
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
- **`handoffs/` shares the archive retention clock.** With `archive.retention_days` above 0, an
  unscoped `rl prune --delete` and `rl roundup` remove `handoffs/*.md` older than that; at the
  default 0 nothing removes them. Files are
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
