# Tooling & Packaging — Design Spec

**Date:** 2026-08-02
**Status:** Approved design, pre-implementation
**Author:** brainstormed with Claude

## One-line

Turn ringleader from a repo-local bash dispatcher into a properly packaged, installable
Python tool (splashdown-parity dev tooling), move all runtime state out of the repo into the
home directory, add a stable `--json` contract for a future app, and isolate the
Claude-Code-specific scanning behind a source seam so a second agent/harness is additive.

## Motivation

Ringleader was just open-sourced. Today it runs as `./rl <sub>` — a bash dispatcher that
shells into 19 `bin/rl-*` scripts, each doing its own `sys.path.insert` to import the
`ringleader/` package, with `registry.json` and `archive/` living **inside the repo**. That's
fine for a personal script but wrong for an installed OSS tool: "install" has no clean
meaning, state pollutes the working tree, and there's no lint/CI story.

Splashdown (`~/wrksp/oss/splashdown`) is the reference for how a mature version looks:
`pyproject.toml` + `uv`, a real console entry point (`splash = "splashdown:main"`), ruff +
mypy, pytest + coverage, a `Justfile`, and CI. This spec ports the parts that fit ringleader's
current ambition.

Two forward-looking facts shape the architecture (but are **not built here**):

- **A future app** ("circus", juggler-style) will show/monitor/triage sessions across all
  projects. It consumes the engine — hence a stable `--json` output and a clean importable
  library core.
- **Multiple harnesses** are coming (Codex, opencode, Gemini/antigravity, Pi, …). Today the
  scanner is hardwired to Claude Code's `~/.claude/projects/*.jsonl`. The architecture must
  make a second source additive, not a rewrite.

## Scope

**In scope (scope "A" — installed personal tool):**

1. Package + console entry point; fold `bin/rl-*` into the package.
2. A `sources/` seam isolating Claude-Code specifics behind one interface.
3. `--json` output on the query commands (`overview`, `triage`).
4. State → `~/.local/share/ringleader/` (registry + archive), with auto-migration.
5. Lint/format (ruff, CI-gating) + optional mypy.
6. Testing config in `pyproject.toml`, plus pytest-cov.
7. A trimmed `Justfile` (install / refresh / reset / check).
8. `ci.yml` (lint + format-check + tests).
9. Ripple updates: skills, README, AGENTS, SPEC.

**Explicitly out of scope (YAGNI — the design enables them later, we don't build them now):**

- Any release/distribution pipeline: no `release.yml`, Homebrew tap, git-cliff `CHANGELOG.md`,
  version bumping, or docs site.
- The second harness source itself (only the seam + the Claude Code source ship).
- The app, and any watcher/daemon mode (watching stays the app's job).
- A full abstraction of name-application and archive file-moves across harnesses (they remain
  Claude-Code-shaped; the seam boundary is noted for when a second harness actually lands).

## Design

### 1. Packaging & the dispatcher refactor

- Add **`pyproject.toml`** (hatchling build backend), `requires-python = ">=3.13"`,
  `dependencies = []` (the package is stdlib-only), `license = "MIT"`, and:
  ```toml
  [project.scripts]
  rl = "ringleader.cli:main"

  [dependency-groups]
  dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.6"]
  ```
- Keep the **flat `ringleader/` layout** at repo root (hatchling `packages = ["ringleader"]`) —
  no `src/` move, to minimize churn.
- Adopt **`uv`** (commit `uv.lock`). Dev = `uv run rl …` / `uv run pytest`; global install =
  `uv tool install .`.
- **Fold the 19 `bin/rl-*` scripts into the package.** New `ringleader/cli.py`:
  - `main(argv=None)` parses the first token as the subcommand and dispatches
    `rl <sub>` → a `cmd_<sub>(args)` callable in `ringleader/commands/` (one module per current
    script, or grouped). Each old `bin/rl-X` `main()` body becomes its `cmd_x`.
  - Subcommand names map 1:1 to today's set: `overview, triage, prune, export, archive, delete,
    mark-harvested, mark-reviewed, cleaned, relocate, snooze, wake, sync, name, tokens,
    mem-scan, mem-prune, mem-cleaned, mark-mem-reviewed`.
  - `python -m ringleader <sub>` works too (`ringleader/__main__.py` → `cli.main`).
- **Delete** `bin/` and the `./rl` bash dispatcher. The `sys.path.insert` hacks disappear
  (the package is importable because it's installed / on `uv`'s path).

### 2. The `sources/` seam (multi-harness-ready)

Isolate everything Claude-Code-specific behind one boundary so registry/classification/views
never see a raw `.jsonl` or the `~/.claude` layout.

```
ringleader/
├── sources/
│   ├── base.py          # the Source protocol + the NormalizedSession dataclass
│   └── claude_code.py    # the ONLY source today
├── scanner.py            # iterates the registered sources → NormalizedSession records
├── registry.py, overview.py, triage.py, naming.py, …  # consume NormalizedSession only
```

- **`sources/base.py`** defines:
  ```python
  @dataclass
  class NormalizedSession:
      source: str            # "claude-code"
      session_id: str
      cwd: str | None        # first recorded cwd → git project resolved by gitinfo (shared)
      mtime: str
      size_bytes: int
      custom_title: str | None
      summary: str | None
      last_prompt: str | None
      subagent_count: int
      file: str              # absolute path to the transcript (source-owned)

  class Source(Protocol):
      name: str
      def discover(self) -> Iterator[RawRef]: ...       # cheap: paths + mtime/size, no parse
      def parse(self, ref: RawRef) -> NormalizedSession: ...  # read tail, extract fields
  ```
- **`sources/claude_code.py`** owns: the `~/.claude/projects` location, the encoded-cwd folder
  naming, the JSONL record types (`custom-title` / `summary` / `last-prompt` / `cwd`), the
  `subagents/` layout, and `.session-aliases`. Today's `scanner.py` enumeration + `transcript.py`
  parsing move here (or become CC-source internals).
- **`scanner.py`** holds `SOURCES = [ClaudeCodeSource()]` and yields normalized records across
  all of them (just one now). `refresh`/reconcile and everything downstream operate on
  `NormalizedSession`.
- **Generic vs CC-specific:** git resolution (`gitinfo.resolve_git_project(cwd)`) is generic and
  stays shared (any harness has a cwd). A session's identity, status, name, prompts, age, size
  are generic. The transcript format, file locations, and folder encoding are CC-specific and
  live only in the source.
- **Noted boundary, not built:** applying a name (writing a `custom-title` record) and archiving
  (moving files under `~/.claude/projects`) are also harness-specific. They stay Claude-Code-shaped
  for now; when a second source lands they join the `Source` interface. We do **not** abstract
  them on a sample size of one.

### 3. `--json` output (the app-facing contract)

The engine already separates **compute** (`overview.compute_metrics`, `overview.triage_inbox`,
project-detail, rollup) from **render** (`render_*`). `--json` is a second serializer on the same
computed model — not a parallel path.

- `rl overview [--json]` and `rl triage [--json]` emit the computed structure as JSON to stdout;
  default output stays human text. The pattern is established so `tokens`/others can adopt it.
- **The JSON shape is the stable contract** the future app depends on: a normalized view
  (`session_id`, `project`, `status`, `name`, `what_it_is`, `mtime`, `age_days`, `size_bytes`,
  flags like blocked/stale/unclassified…), **not** the raw `registry.json` internals. It is
  documented in the README and treated as a public interface (breaking changes are deliberate).
- A single helper (`ringleader/jsonout.py`) turns the computed models into JSON-safe dicts, so
  there is one place that defines the contract.

### 4. State → home directory

- New **`ringleader/paths.py`**:
  ```python
  def state_dir() -> Path:
      base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
      return Path(base) / "ringleader"
  ```
  It holds **`registry.json`** and **`archive/`**. `DEFAULT_REGISTRY = state_dir() / "registry.json"`;
  the archive root becomes `state_dir() / "archive"`. The existing `--registry <path>` override stays
  (tests pass a temp path through it).
- **Migration** (`ringleader/migrate.py`, exposed as `rl migrate [<old-dir>]`): if
  `state_dir()/registry.json` does not exist but a legacy `registry.json` (and/or `archive/`) is
  found — in `<old-dir>` if given, else the current directory — move them into `state_dir()`.
  Idempotent: a no-op once state already lives in home. It is run **from the repo before the global
  install**, via `uv run rl migrate` (which executes the source entry point, so it works before
  `rl` is on `PATH`) — this ordering matters because after a global install the user no longer runs
  `rl` from the repo, so the legacy state must be relocated at install time, not lazily on startup.
- **Remove `harvests/`** — it is dead scaffolding. The real harvest flow writes into the *target*
  repo's docs (via `/doc`) and records the doc paths in the registry's `harvestPath`; nothing writes
  to a ringleader `harvests/` dir. Delete the directory and its `.gitkeep`; the `harvestPath`
  registry field (which points at target-repo docs) is unaffected.
- `.gitignore` keeps `registry.json` / `archive/` ignored defensively (in case the tool is run from
  the repo before an install migrates state), even though they no longer normally appear there.

### 5. Lint / format

- **ruff** (lint + format), configured in `pyproject.toml`, porting splashdown's rule set trimmed
  to ringleader: `select = ["E","F","W","I","UP","B","C4","SIM","PL","RUF"]`, `line-length = 100`,
  `ignore = ["E501"]` (formatter owns width), per-file-ignores for `tests/*`
  (`S101,SLF001,PLR2004,E402,PLC0415`) and `**/__init__.py` (`F401,E402`). Pylint complexity
  thresholds tuned for a CLI (`max-branches`, `max-statements`, `max-args` as splashdown does).
  **ruff gates CI.**
- **mypy: optional, not CI-gating.** The codebase is largely untyped; strict typing is a separate
  annotation project. Add a `just typecheck` recipe and a lenient `[tool.mypy]` block so it can be
  adopted gradually, but do not block CI on it initially.

### 6. Testing

- Keep pytest. **Fold `pytest.ini` into `pyproject.toml`** (`[tool.pytest.ini_options]`,
  `testpaths = ["tests"]`, `pythonpath = ["."]`). Delete `pytest.ini`.
- Add **pytest-cov** with a `[tool.coverage]` block; coverage reported (`term-missing`) but
  **ungated to start** (or a modest `fail_under`, ≤ 70), so the port doesn't turn into a
  coverage-chasing task. Raise later if desired.
- **Test invocation change:** tests that today subprocess `bin/rl-*` or `REPO/"rl"`
  (`tests/test_rl_dispatch.py`, `test_rl_curation.py`, `test_rl_*`) switch to invoking the package —
  `python -m ringleader <sub>` (or importing the `cmd_*` functions directly, preferred for unit
  tests). This is part of the refactor and must stay green.

### 7. Justfile (trimmed to scope A)

```
default        # just --list
test           # uv run pytest -q
test-verbose   # uv run pytest -v
cov            # uv run pytest --cov --cov-report=term-missing -q
lint           # uv run ruff check
fmt            # uv run ruff format
check          # ruff check && ruff format --check && pytest   (what CI runs)
typecheck      # uv run mypy   (optional; not in `check`)
install-local  # uv run rl migrate  &&  uv tool install .     → global `rl`
refresh-local  # uv tool install --reinstall --force .
reset-local    # uv tool uninstall ringleader
clean          # rm caches / .pytest_cache / .coverage
```

Deliberately **omit** splashdown's `build / docs / demo / changelog / release / tag-release / hooks`.

### 8. CI

- **`.github/workflows/ci.yml` only.** On push / PR to `main`: check out, install `uv`, set up
  Python 3.13, `uv sync`, run `just check` (ruff check + ruff format --check + pytest).
- **`ubuntu-latest`** — tests use temp dirs and never touch a real `~/.claude`, so no macOS runner
  is needed. **No** `docs.yml` / `release.yml`.

### 9. Ripples

- **Skills:** the 6 `.claude/skills/*/SKILL.md` invoke `.venv/bin/python bin/rl-x` today; they
  become the installed `rl x` (or `uv run rl x` for a from-source dev checkout). Update every
  invocation + the "run from the repo" preambles.
- **README / AGENTS / SPEC:** document the new install (`uv tool install .` / `just install-local`),
  the `rl` command (no more `./rl`), the home state directory, `--json`, and the `sources/` seam.
  AGENTS' "Gotchas" and "Commands" sections updated accordingly.

## Rollout order (rough — the plan will detail this)

1. `pyproject.toml` + `uv.lock`; get `uv run pytest` green against the current package.
2. `ringleader/cli.py` + `commands/` dispatcher; port each `bin/rl-*` body into a `cmd_*`; wire
   the `rl` entry point; update tests to the new invocation; delete `bin/` + `./rl`.
3. `sources/` seam: extract `scanner.py`/`transcript.py` CC-specifics into
   `sources/claude_code.py` behind the `Source` protocol; downstream consumes `NormalizedSession`.
4. `paths.py` + state-in-home + `migrate`; delete `harvests/`.
5. `--json` on `overview`/`triage` via `jsonout.py`.
6. ruff config + fix findings; pytest-cov; fold pytest config into pyproject.
7. `Justfile`; `ci.yml`.
8. Skills / README / AGENTS / SPEC updates.

Each step keeps `pytest` (and, from step 6, `ruff`) green.

## Decisions locked (from brainstorming)

- Language stays **Python** (I/O-bound workload; Textual/Rich make a future TUI a strength, not a
  reason to switch; a working tested implementation exists; the app is a *separate* layer).
- Scope is **A** (installed personal tool) — no release pipeline yet; easy to add later.
- Install is a **real package via `uv tool install`**, not a symlink wrapper.
- State goes to **`~/.local/share/ringleader/`**; `harvests/` is deleted.
- `--json` is **in** this port; the second harness source, the watcher, and the app are **out**.

## Open questions

None blocking. Two defaults chosen (flip during the plan if desired): mypy is optional /
non-gating; coverage is reported but ungated (or ≤70) to start.
