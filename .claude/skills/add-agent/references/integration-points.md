# Where an agent plugs into ringleader

Every place that names an agent, grouped by tier. Paths are repo-relative; a symbol after the
colon is the thing to extend. The list was taken by grepping for `droid` in November 2026, so
regrep before trusting it: `grep -rn -i '"droid"\|droid' ringleader scripts tests`. A hit that
names droid but not the new agent is a place to decide on.

Agent names: the source name is what the registry stores (`claude-code`, `codex`, `cursor`,
`droid`, `opencode`, `pi`). Claude Code alone has a different display and binary name, `claude`
(`ringleader/agents.py`). Pick one lowercase name and use it everywhere.

## Tier 0: presence (live sessions)

Live sessions come from the bundled hooklinesinker, so this tier is only ringleader's half.
hooklinesinker must already report the agent.

- `ringleader/presence.py`: `AGENTS` and the `homes` map in `detected_agents()`. A documented
  home-override env var goes through `_agent_home_override()`; never hardcode `root / ".x"` when
  the agent has an override.
- `tests/conftest.py`: `isolate_agent_environment` clears that override variable.
- `tests/test_presence.py`, `tests/test_rl_presence.py`.

qwen and kimi stop here.

## Tier 1: sessions (scan, triage, inbox, resume, start, lifecycle)

The core. See `docs/tech/sources.md` for the protocol and every capability.

- `ringleader/sources/<agent>.py`: the `Source` class. `discover()` yields `RawRef`s;
  `parse()` returns `NormalizedSession`; set `parser_version`; cap `tail_text` at 6000
  characters. Exclude subagent or exec runs in `discover()` (Codex `_is_interactive`, droid
  `callingSessionId` and the exec sidecar tag).
- Capabilities: declare only what is verified. Each needs a protocol method and the string in
  `capabilities`:
  - `launches_sessions`: `resume_argv`, `fork_argv`, `launch_argv`. No directory in argv.
  - `forks_sessions`: only with a native fork verb. Without it, `fork_argv` raises and the
    picker offers no fork or worktree row (Cursor).
  - `filesystem_lifecycle`: sessions are movable files, so archive, delete and prune apply.
    Leave it off for a database the agent owns (OpenCode).
  - `companion_files`: sidecars that must travel with the transcript. `companion_destination`
    says where each goes when the transcript moves; for state kept in another root, derive it
    from the state itself (Cursor reads the cwd from the archived `meta.json`).
  - `relocates_workspace`: storage is keyed by the launch directory. `storage_dir` maps a
    transcript back to that directory when it is not the transcript's parent, and
    `stored_transcripts` lists the transcripts in it.
  - `store_roots` (`SplitStoreSource`, no capability string): every tree the sessions span, when
    there is more than one. `delete` and `prune` confine paths to them.
  - `rebinds_workspace`: the cwd lives in an index the picker filters on.
  - `hides_from_picker` / `shows_in_picker`: only with a verified, writable index schema.
  - `applies_names`: only with an append-style rename record.
- `ringleader/scanner.py`: `SOURCE_TYPES`, and the `factories` map in `source_for_thread()`
  (recorded native stores). The archive names files by their filename and purge accepts only
  `.jsonl`, so the session file needs a unique `.jsonl` name.
- `ringleader/slug.py`: a separate encoder if the agent's directory encoding differs from Claude
  Code's (`droid_slug`). Verify it against every real session, not a sample.
- `ringleader/resume.py`: `drifted()` and `launch_cwd()` for slug-keyed storage.
- `ringleader/worktrees.py`: the supported-source set in the plan builder.
- `ringleader/commands/start.py`, `ringleader/commands/resume.py`: special cases for how the
  agent takes a prompt (OpenCode `--prompt`, Pi `--`).
- `ringleader/session_env.py`: `PROBES`, if the agent exports its session id in the
  environment. This is what `--current` and `ringleader-finish` use. Droid has none.
- `ringleader/export.py`: `render_conversation` record shapes. Check whether an existing shape
  already renders the agent before writing a renderer.
- `ringleader/paths.py`: a home helper if messages must name the agent's config directory.
- Test isolation, before anything else: `ringleader/devtools/isolation.py` `AGENT_STORE_VARS`,
  which conftest, `scripts/sandbox.py`, `scripts/tui_shot.py`, `devtools/tui_fixture.py` (and so
  `gui_world`) and `tests/tui_e2e.py` all read. Add the agent's home dir to
  `gui_world.FORBIDDEN_HOME_DIRS`.
- Test tables that enumerate agents: `tests/test_scanner.py` exact `active_sources()` sets,
  `tests/test_sources_launch.py` argv tables, `tests/test_rl_resume.py`, `tests/test_rl_start.py`,
  `tests/test_worktrees.py`, `tests/test_sources.py`. New: `tests/test_sources_<agent>.py`.
- `ringleader/devtools/tui_fixture.py`: `SOURCE_WEIGHTS` and the store layout. Changing the
  weights reseeds the world: every inbox golden and the contract goldens circus mirrors byte for
  byte change. Cursor left it alone; only do it with the user's agreement.
- `tests/golden/contract-list.json`: the `agents` list follows `SOURCE_TYPES`, so a new source
  adds an entry there (`RINGLEADER_UPDATE_GOLDEN=1`). Tell the circus side it must re-sync.

## Tier 2: usage (`rl usage`)

- `ringleader/usage/<agent>.py`: a `UsageReader`. Find where tokens actually live: droid keeps
  them in the `.settings.json` sidecar, not the transcript.
- `ringleader/usage/aggregate.py`: `READERS`, and `ARCHIVE_KWARG` if archived sessions keep
  their usage readable.
- `ringleader/usage/pricing.py`: model prices, or a note that the agent bills credits and is
  never priced.
- Count each token once. Droid's review overturned "inclusive" attribution because archiving a
  parent double-counted its children; attribute each session by its own usage.
- Tests: `tests/test_usage_<agent>.py` with a malformed-file case, `tests/test_usage_aggregate.py`,
  `tests/test_rl_usage.py`.

## Tier 3: hooks and skills (`rl install`)

- `ringleader/commands/hooks.py`: a `JsonAgent` in `_json_agents()`, with its file shape and
  hook timeout. Preserve user hooks, and never delete a file ringleader did not create.
- `ringleader/commands/skills.py`: `_install_dirs()`. Use the agent's own personal skills dir
  if it has one (droid uses `~/.factory/skills`, not `~/.agents/skills`).
- The "no supported agents detected" prose in both commands.
- Tests: `tests/test_rl_hooks.py`, `tests/test_skills_cmd.py`, `tests/test_doctor.py`, all
  against a temp `HOME`.

## Tier 4: native conversion (`rl resume --to`, `rl session convert`)

Large. Read `docs/tech/session-portability.md` and
`docs/reference/session-portability-native.md` first.

- `ringleader/portability/<agent>.py`: the adapter (reader, writer, launcher roles).
- `ringleader/portability/verify/<agent>.py`: the offline self-verification harness. Loopback
  only, no real inference, safe unattended.
- `ringleader/portability/adapters.py`: `builtin_adapters()` registration with its `RootLayout`.
- `ringleader/portability/verification.py`: `HARNESS_VERSION` and `TIERS`.
- `ringleader/portability/native.py`: agent-specific launch quirks, such as droid's login flow
  guard.
- `ringleader/commands/conversion.py`: the `--to` help text.
- `scripts/conversion-sweep.py`: the expected "no conversation" messages.
- Tests under `tests/portability/`.

Without this tier, `rl resume --to <agent>` still works through the seeded Markdown handoff.

## Tier 5: analysis (`rl analyze`)

- `ringleader/commands/analyze.py`: `_AGENTS`. An agent listed there but missing from
  `analysis/adapters.py:ADAPTERS` gets token-only analysis.
- `ringleader/analysis/render.py`: `SOURCES`.
- Full analysis needs an `AnalysisAdapter` in `ringleader/analysis/adapters.py` and an extractor
  module (`analysis/codex.py` is the model). New facts shape means bumping `FACTS_VERSION`
  (see AGENTS.md).

## Docs and shipped skills

- `docs/reference/agent-session-management.md`: a `## <Agent>` section, a column in the
  Summary table, and a "verified YYYY-MM-DD against <agent> X.Y.Z" line at the top. Mark
  anything not observed as *(unverified)*.
- `docs/tech/sources.md`: the implementations list and any new gotcha.
- Every agent count or list ("six agents", "claude/codex/droid/opencode/pi") in `README.md`,
  `AGENTS.md`, `docs/features/`, `docs/tech/`, `docs/user/` and `skills/`. Grep for an
  existing agent's name and for the spelled-out count.
- `skills/PLAYBOOK.md`, then `just sync-playbook` to copy it into each skill's
  `references/playbook.md`. Never edit the copies.
- `skills/SMOKE.md`: a row for the agent.
- `tests/golden/contract-*.json` only if the JSON contract output changes; that needs
  `docs/tech/json-contract.md` updated too, since circus consumes it.
