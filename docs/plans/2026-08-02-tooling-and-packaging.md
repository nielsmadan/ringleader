# Ringleader Tooling & Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn ringleader from a repo-local bash dispatcher into an installable Python package with a `sources/` seam, `--json` output, home-directory state, and splashdown-parity dev tooling (ruff / pytest+cov / Justfile / CI).

**Architecture:** Fold the 19 `bin/rl-*` scripts into a `ringleader.cli:main` console entry point; isolate all Claude-Code-specific scanning behind a `Source` protocol that yields a normalized session model; move `registry.json` + `archive/` to `~/.local/share/ringleader/`; add `--json` serialization on the same computed models the text renderers already use.

**Tech Stack:** Python 3.13 (stdlib-only runtime), `uv` + hatchling, ruff, pytest + pytest-cov, `just`, GitHub Actions.

## Global Constraints

- **Runtime is stdlib-only** — `dependencies = []`. Dev-only tools go in the `dev` dependency group. (spec §1)
- **`requires-python = ">=3.13"`.** (spec §1)
- **Entry point:** `rl = "ringleader.cli:main"`; `python -m ringleader <sub>` must also work. (spec §1)
- **No new runtime dependency** may be introduced by any task (including `--json`, which uses stdlib `json`). (spec §3)
- **Downstream code consumes `NormalizedSession` only** — no `.jsonl`, `~/.claude` paths, or CC record types outside `ringleader/sources/claude_code.py` + the transcript parser. (spec §2)
- **Keep `pytest` green after every task; keep `ruff` clean from Task 6 on.** (spec §6)
- **Commit style:** conventional `feat/fix/chore/test/docs`, no scopes, terse (repo convention).
- **Out of scope — do not build:** release pipeline, second harness source, watcher/daemon, name-application/archive abstraction, docs site. (spec Scope)

---

## File Structure

**Created:**
- `pyproject.toml` — build system, metadata, entry point, dev deps, pytest/coverage/ruff config
- `ringleader/cli.py` — `main()` + subcommand dispatch table
- `ringleader/__main__.py` — `python -m ringleader` → `cli.main`
- `ringleader/commands/__init__.py` + `ringleader/commands/<sub>.py` — one `cmd_<sub>(argv)` per old `bin/rl-*`
- `ringleader/sources/__init__.py`, `sources/base.py` (`Source`, `NormalizedSession`, `RawRef`), `sources/claude_code.py`
- `ringleader/paths.py` — `state_dir()`
- `ringleader/migrate.py` — legacy-state relocation
- `ringleader/jsonout.py` — computed-model → JSON-safe dicts
- `Justfile`, `.github/workflows/ci.yml`, `uv.lock`

**Modified:**
- `ringleader/scanner.py` — iterate `SOURCES`; CC specifics leave for `sources/claude_code.py`
- `ringleader/registry.py`, `overview.py`, `refresh.py` — consume `NormalizedSession`; registry/archive paths from `paths.py`
- `tests/*` — call the package (`python -m ringleader` / import `cmd_*`) instead of `bin/rl-*`
- `.claude/skills/*/SKILL.md`, `README.md`, `AGENTS.md`, `SPEC.md`, `.gitignore`

**Deleted:** `bin/`, `rl` (bash dispatcher), `pytest.ini`, `harvests/`

---

### Task 1: Packaging skeleton (pyproject + uv, tests green under uv)

**Files:**
- Create: `pyproject.toml`, `uv.lock`
- Delete: `pytest.ini`

**Interfaces:**
- Produces: an installable project named `ringleader`; `uv run pytest` as the test entry; the
  `rl = "ringleader.cli:main"` entry point declaration (target `cli.py` arrives in Task 2).

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ringleader"
version = "0.1.0"
description = "Curate dormant Claude Code sessions on disk — name, classify, triage, and prune them before the GC clock deletes them."
readme = "README.md"
requires-python = ">=3.13"
dependencies = []
license = "MIT"
authors = [{ name = "Niels Madan" }]

[project.scripts]
rl = "ringleader.cli:main"

[dependency-groups]
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.6"]

[tool.hatch.build.targets.wheel]
packages = ["ringleader"]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
addopts = "-q"
pythonpath = ["."]

[tool.coverage.run]
source = ["ringleader"]
branch = true

[tool.coverage.report]
show_missing = true
```

> Note: the PyPI name `ringleader` is taken, but this project installs only from local source
> (`uv tool install .`), where the name is a local identifier — no PyPI publish is in scope.

- [ ] **Step 2: Delete `pytest.ini`** (its settings now live in `[tool.pytest.ini_options]`).

```bash
git rm pytest.ini
```

- [ ] **Step 3: Generate the lockfile and run tests under uv**

Run: `uv lock && uv run pytest`
Expected: PASS — the same suite that passed before, now resolved through `pyproject.toml`.
(The `rl` entry point is declared but its module doesn't exist yet; that's fine — `uv run pytest` does not import it.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add pyproject packaging and uv, fold pytest config"
```

---

### Task 2: CLI dispatcher — fold `bin/rl-*` into the package

**Files:**
- Create: `ringleader/cli.py`, `ringleader/__main__.py`, `ringleader/commands/__init__.py`, `ringleader/commands/<sub>.py` (one per subcommand)
- Modify: `tests/test_rl_dispatch.py`, `tests/test_rl_curation.py`, `tests/test_rl_export.py`, `tests/test_rl_name.py`, `tests/test_rl_overview.py`, `tests/test_rl_prune.py`, `tests/test_rl_triage.py`, `tests/test_rl_mem.py` (switch invocation)
- Delete: `bin/`, `rl`

**Interfaces:**
- Produces: `ringleader.cli.main(argv: list[str] | None = None) -> int`; and for every subcommand,
  `ringleader.commands.<sub>.cmd_<sub>(argv: list[str]) -> int`.
- Consumes: the existing `ringleader/*.py` modules (now via relative imports, no `sys.path` hack).

**Subcommand set (19), each becomes `cmd_<sub>` in `commands/<name>.py`:**
`overview, triage, prune, export, archive, delete, mark-harvested, mark-reviewed, cleaned,
relocate, snooze, wake, sync, name, tokens, mem-scan, mem-prune, mem-cleaned, mark-mem-reviewed`
→ module/function names replace `-` with `_` (e.g. `mark-harvested` → `commands/mark_harvested.py`,
`cmd_mark_harvested`).

**Port recipe (apply to each `bin/rl-<sub>` verbatim):**
1. Drop the `REPO = Path(__file__)…` + `sys.path.insert(...)` preamble.
2. Change `from ringleader import X` / bare imports to package-relative: `from ..registry import …`, `from ..overview import …`.
3. Rename `def main():` → `def cmd_<sub>(argv: list[str]) -> int:`.
4. Change the parser prog and parse call: `argparse.ArgumentParser(prog="rl <sub>")` and `args = ap.parse_args(argv)`.
5. Every `sys.exit(N)` becomes `return N`; the success path ends `return 0`.

- [ ] **Step 1: Write the failing dispatcher test** (`tests/test_rl_dispatch.py` — replace the subprocess-on-`./st`/`rl` version)

```python
from ringleader.cli import main

def test_unknown_command_returns_2(capsys):
    rc = main(["definitely-not-a-command"])
    assert rc == 2
    assert "unknown command" in capsys.readouterr().err

def test_help_lists_commands(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "overview" in out and "triage" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_rl_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError: ringleader.cli`.

- [ ] **Step 3: Write `ringleader/cli.py`**

```python
from __future__ import annotations

import sys

from . import commands

# name -> callable(argv) -> int
DISPATCH = commands.TABLE


def _usage(stream=sys.stdout) -> None:
    print("ringleader — usage: rl <command> [args]\n", file=stream)
    print("commands: " + "  ".join(sorted(DISPATCH)), file=stream)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    sub, rest = argv[0], argv[1:]
    fn = DISPATCH.get(sub)
    if fn is None:
        print(f"rl: unknown command '{sub}'", file=sys.stderr)
        _usage(sys.stderr)
        return 2
    return fn(rest) or 0
```

- [ ] **Step 4: Write `ringleader/commands/__init__.py`** — the `TABLE` mapping every subcommand string to its `cmd_*`.

```python
from . import (
    overview, triage, prune, export, archive, delete, mark_harvested,
    mark_reviewed, cleaned, relocate, snooze, wake, sync, name, tokens,
    mem_scan, mem_prune, mem_cleaned, mark_mem_reviewed,
)

TABLE = {
    "overview": overview.cmd_overview,
    "triage": triage.cmd_triage,
    "prune": prune.cmd_prune,
    "export": export.cmd_export,
    "archive": archive.cmd_archive,
    "delete": delete.cmd_delete,
    "mark-harvested": mark_harvested.cmd_mark_harvested,
    "mark-reviewed": mark_reviewed.cmd_mark_reviewed,
    "cleaned": cleaned.cmd_cleaned,
    "relocate": relocate.cmd_relocate,
    "snooze": snooze.cmd_snooze,
    "wake": wake.cmd_wake,
    "sync": sync.cmd_sync,
    "name": name.cmd_name,
    "tokens": tokens.cmd_tokens,
    "mem-scan": mem_scan.cmd_mem_scan,
    "mem-prune": mem_prune.cmd_mem_prune,
    "mem-cleaned": mem_cleaned.cmd_mem_cleaned,
    "mark-mem-reviewed": mark_mem_reviewed.cmd_mark_mem_reviewed,
}
```

- [ ] **Step 5: Port each `bin/rl-<sub>` into `commands/<sub>.py`** using the recipe above. Worked example — `commands/triage.py` (from `bin/rl-triage`):

```python
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from .. import overview, projects, refresh, registry
from ..paths import default_registry   # added in Task 4; until then use the old REPO/registry.json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_triage(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl triage",
                                 description="List sessions awaiting your decision.")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--unclassified", "-u", action="store_true")
    # (--memory branch unchanged from bin/rl-triage)
    args = ap.parse_args(argv)
    now = _now()
    reg = (registry.load_registry(args.registry) if args.no_scan
           else refresh.auto_refresh(args.registry, now)[0])
    threads = projects.filter_by_projects(reg["threads"], args.project)
    print(overview.render_triage(threads, now, unclassified_only=args.unclassified))
    return 0
```

> Do the same mechanical port for the other 18. Keep each module small and single-purpose.

- [ ] **Step 6: Write `ringleader/__main__.py`**

```python
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Update tests that invoked the old scripts.** Replace subprocess calls to `bin/rl-*` / `REPO/"rl"` with either a direct import (preferred) or `python -m ringleader`. Example diff pattern in `tests/test_rl_curation.py`'s `_run` helper:

```python
# before: subprocess.run([str(PY), str(REPO / "bin" / f"rl-{name}"), ...])
# after:
import subprocess, sys
def _run(name, reg, *args):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", name, "--registry", str(reg), *args],
        capture_output=True, text=True,
    )
```

- [ ] **Step 8: Delete the old dispatcher + scripts**

```bash
git rm -r bin rl
```

- [ ] **Step 9: Verify entry point + suite**

Run: `uv run rl overview --no-scan --registry /tmp/none.json ; uv run pytest`
Expected: the `rl` command runs (prints an empty/new-registry overview), and the full suite PASSES.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: single rl entry point, fold bin scripts into ringleader.commands"
```

---

### Task 3: `sources/` seam — isolate Claude Code specifics

**Files:**
- Create: `ringleader/sources/__init__.py`, `ringleader/sources/base.py`, `ringleader/sources/claude_code.py`
- Modify: `ringleader/scanner.py`, `ringleader/registry.py` (reconcile), any module that read CC-specific fields directly
- Test: `tests/test_sources.py` (new), `tests/test_scanner.py` (retarget to the normalized shape)

**Interfaces:**
- Produces:
  ```python
  # sources/base.py
  @dataclass(frozen=True)
  class RawRef:
      source: str
      file: str          # absolute transcript path
      mtime: str
      size_bytes: int

  @dataclass
  class NormalizedSession:
      source: str
      session_id: str
      cwd: str | None
      mtime: str
      size_bytes: int
      custom_title: str | None
      summary: str | None
      last_prompt: str | None
      subagent_count: int
      file: str

  class Source(Protocol):
      name: str
      def discover(self) -> Iterator[RawRef]: ...
      def parse(self, ref: RawRef) -> NormalizedSession: ...
  ```
- Consumes: existing `transcript.parse_transcript` + the `~/.claude/projects` enumeration (both move under `claude_code.py`).

- [ ] **Step 1: Write the failing source test** (`tests/test_sources.py`)

```python
import json
from pathlib import Path
from ringleader.sources.claude_code import ClaudeCodeSource

def test_parse_extracts_normalized_fields(tmp_path: Path):
    proj = tmp_path / "-Users-me-wrksp-proj"
    proj.mkdir()
    f = proj / "aaaaaaaa-0000-0000-0000-000000000000.jsonl"
    f.write_text("\n".join([
        json.dumps({"type": "user", "cwd": "/Users/me/wrksp/proj", "message": {"content": "hi"}}),
        json.dumps({"type": "custom-title", "customTitle": "my-thread"}),
    ]))
    src = ClaudeCodeSource(projects_dir=tmp_path)
    refs = list(src.discover())
    assert len(refs) == 1
    ns = src.parse(refs[0])
    assert ns.source == "claude-code"
    assert ns.session_id == "aaaaaaaa-0000-0000-0000-000000000000"
    assert ns.cwd == "/Users/me/wrksp/proj"
    assert ns.custom_title == "my-thread"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_sources.py -v`
Expected: FAIL (`ModuleNotFoundError: ringleader.sources`).

- [ ] **Step 3: Write `sources/base.py`** — the dataclasses + `Source` protocol from the Interfaces block.

- [ ] **Step 4: Write `sources/claude_code.py`** — move the `~/.claude/projects` enumeration out of `scanner.py` and the JSONL field extraction out of `transcript.py` into `ClaudeCodeSource(projects_dir: Path = default_projects_dir())`, implementing `discover()`/`parse()` and returning `NormalizedSession(source="claude-code", …)`. Keep the `/clear`-boundary and subagent-count logic intact — just relocated.

- [ ] **Step 5: Rewire `scanner.py`** to a source loop:

```python
from .sources.claude_code import ClaudeCodeSource

SOURCES = [ClaudeCodeSource()]

def scan() :
    for src in SOURCES:
        for ref in src.discover():
            yield src.parse(ref)
```

Update `registry.reconcile` (and anything downstream) to read `NormalizedSession` attributes
(`ns.session_id`, `ns.cwd`, `ns.custom_title`, …) rather than CC dict keys. `gitinfo.resolve_git_project(ns.cwd)` stays as the shared cwd→project step.

- [ ] **Step 6: Run tests to verify green**

Run: `uv run pytest -v`
Expected: PASS (new source test + retargeted `test_scanner.py` + everything else).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: isolate Claude Code scanning behind a Source seam"
```

---

### Task 4: State → home, migration, delete `harvests/`

**Files:**
- Create: `ringleader/paths.py`, `ringleader/migrate.py`, `ringleader/commands/migrate.py`
- Modify: every `commands/*.py` `--registry` default; archive-root resolution in `commands/archive.py` + `overview.py` (`--archived`); `commands/__init__.py` TABLE (+`"migrate"`); `.gitignore`
- Delete: `harvests/`
- Test: `tests/test_paths.py`, `tests/test_migrate.py`

**Interfaces:**
- Produces:
  ```python
  # paths.py
  def state_dir() -> Path            # $XDG_DATA_HOME/ringleader else ~/.local/share/ringleader
  def default_registry() -> Path     # state_dir() / "registry.json"
  def archive_root() -> Path         # state_dir() / "archive"
  # migrate.py
  def migrate(old_dir: Path) -> list[str]   # returns moved paths; no-op if state already exists
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_paths.py
import os
from pathlib import Path
from ringleader.paths import state_dir

def test_state_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "ringleader"

def test_state_dir_defaults_to_local_share(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert state_dir() == Path.home() / ".local" / "share" / "ringleader"
```

```python
# tests/test_migrate.py
from ringleader.migrate import migrate

def test_migrate_moves_legacy_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    old = tmp_path / "repo"; old.mkdir()
    (old / "registry.json").write_text('{"threads": []}')
    (old / "archive").mkdir()
    moved = migrate(old)
    assert (tmp_path / "home" / "ringleader" / "registry.json").exists()
    assert moved  # non-empty
    # idempotent second run is a no-op
    assert migrate(old) == []
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_paths.py tests/test_migrate.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `paths.py`**

```python
from __future__ import annotations
import os
from pathlib import Path

def state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ringleader"

def default_registry() -> Path:
    return state_dir() / "registry.json"

def archive_root() -> Path:
    return state_dir() / "archive"
```

- [ ] **Step 4: Write `migrate.py`** — if `default_registry()` is absent and `old_dir/registry.json` (and/or `old_dir/archive/`) exist, `state_dir().mkdir(parents=True, exist_ok=True)` then `shutil.move` them in; return the moved paths (empty list when state already exists).

- [ ] **Step 5: Write `commands/migrate.py`** (`cmd_migrate(argv)`: optional positional `old_dir` defaulting to `.`; prints what moved) and add `"migrate": migrate.cmd_migrate` to the TABLE.

- [ ] **Step 6: Point defaults at `paths.py`.** In every `commands/*.py`, change `default=str(DEFAULT_REGISTRY)` → `default=str(default_registry())`. In `commands/archive.py` and the `overview --archived` path, resolve the archive root via `archive_root()`. `--registry`/archive-root overrides stay (tests pass temp paths).

- [ ] **Step 7: Delete `harvests/` and adjust `.gitignore`**

```bash
git rm -r harvests
```
Keep `registry.json` and `archive/` in `.gitignore` (defensive).

- [ ] **Step 8: Run the suite** — `uv run pytest` → PASS (tests inject `--registry`/temp paths, so relocating the default doesn't disturb them; new paths/migrate tests pass).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: move state to ~/.local/share/ringleader with rl migrate; drop harvests"
```

---

### Task 5: `--json` output on `overview` and `triage`

**Files:**
- Create: `ringleader/jsonout.py`
- Modify: `ringleader/commands/overview.py`, `ringleader/commands/triage.py`
- Test: `tests/test_jsonout.py`

**Interfaces:**
- Produces: `jsonout.triage_json(threads, now) -> list[dict]` and `jsonout.rollup_json(reg, now) -> dict`
  (JSON-safe views built from the same computed models the renderers use). Field names are the stable
  contract: `session_id, project, status, name, what_it_is, mtime, age_days, size_bytes, unclassified, blocked, stale`.

- [ ] **Step 1: Write the failing test** (`tests/test_jsonout.py`)

```python
import json, subprocess, sys

def test_triage_json_is_valid_and_shaped(tmp_path):
    reg = tmp_path / "r.json"
    reg.write_text(json.dumps({"version": 1, "lastScan": "2026-08-01T00:00:00+00:00",
        "threads": [{"sessionId": "s1", "status": "unclassified", "mtime": "2026-08-01T00:00:00+00:00",
                     "project": "/p", "whatItIs": "a thing", "snoozedAt": None, "decision": None,
                     "reviewedAtMtime": None, "gitProjectLabel": "repo"}], "tombstones": []}))
    out = subprocess.run([sys.executable, "-m", "ringleader", "triage",
                          "--no-scan", "--registry", str(reg), "--json"],
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    assert isinstance(data, list) and data[0]["session_id"] == "s1"
    assert data[0]["status"] == "unclassified"
```

- [ ] **Step 2: Run to verify it fails** — FAIL (`--json` unknown / not JSON).

- [ ] **Step 3: Write `jsonout.py`** — map the computed structures (`overview.triage_inbox`, rollup metrics/groups) into the contract dicts above, using stdlib `json` only.

- [ ] **Step 4: Add `--json` to `cmd_triage` and `cmd_overview`** — `ap.add_argument("--json", action="store_true")`; when set, `print(json.dumps(jsonout.triage_json(...), indent=2))` and `return 0` before the text-render path. Text output unchanged when the flag is absent.

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_jsonout.py -v` → PASS; full suite green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: add --json output to overview and triage"
```

---

### Task 6: ruff lint + format

**Files:**
- Modify: `pyproject.toml` (`[tool.ruff]` block), plus any files ruff flags

**Interfaces:** none (tooling).

- [ ] **Step 1: Add the ruff config to `pyproject.toml`**

```toml
[tool.ruff]
target-version = "py313"
line-length = 100
src = ["ringleader", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "C4", "SIM", "PL", "RUF"]
ignore = ["E501"]

[tool.ruff.lint.pylint]
max-branches = 15
max-statements = 60
max-args = 6
max-returns = 8

[tool.ruff.lint.per-file-ignores]
"**/__init__.py" = ["F401", "E402"]
"tests/*" = ["S101", "SLF001", "PLR2004", "E402", "PLC0415"]
```

- [ ] **Step 2: Auto-fix + format**

Run: `uv run ruff check --fix && uv run ruff format`

- [ ] **Step 3: Resolve remaining findings by hand** (complexity, unused, shadowing). Re-run until clean.

Run: `uv run ruff check && uv run ruff format --check`
Expected: no errors, no reformatting needed.

- [ ] **Step 4: Confirm tests still pass** — `uv run pytest` → PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: add ruff config and apply lint/format"
```

---

### Task 7: Justfile + CI

**Files:**
- Create: `Justfile`, `.github/workflows/ci.yml`

**Interfaces:** none (tooling).

- [ ] **Step 1: Write `Justfile`**

```make
[private]
default:
    @just --list

test:
    @uv run pytest -q

cov:
    @uv run pytest --cov --cov-report=term-missing -q

lint:
    @uv run ruff check

fmt:
    @uv run ruff format

# Everything CI runs.
check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run pytest -q

# Optional; not part of `check`.
typecheck:
    @uv run mypy ringleader || true

# Migrate legacy repo state, then install the global `rl`.
install-local:
    @uv run rl migrate
    @uv tool install .
    @echo "Installed: $(which rl)"

refresh-local:
    @uv tool install --reinstall --force .

reset-local:
    @uv tool uninstall ringleader

clean:
    @rm -rf .pytest_cache .ruff_cache .coverage htmlcov
```

- [ ] **Step 2: Write `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv python install 3.13
      - run: uv sync
      - run: uv run ruff check
      - run: uv run ruff format --check
      - run: uv run pytest -q
```

- [ ] **Step 3: Verify locally**

Run: `just check`
Expected: ruff clean + pytest PASS (mirrors CI).

- [ ] **Step 4: Commit**

```bash
git add Justfile .github/workflows/ci.yml
git commit -m "chore: add Justfile and CI workflow"
```

---

### Task 8: Docs & skills ripples

**Files:**
- Modify: `.claude/skills/session-sync/SKILL.md`, `session-name/SKILL.md`, `session-overview/SKILL.md`, `session-harvest/SKILL.md`, `session-triage/SKILL.md`, `memory-triage/SKILL.md`; `README.md`, `AGENTS.md`, `SPEC.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Update the skills.** Replace every `.venv/bin/python bin/rl-<sub>` invocation and `cd ~/wrksp/incubation/ringleader && …` preamble with the installed `rl <sub>` (note `uv run rl <sub>` for a from-source dev checkout). Verify none remain:

Run: `git grep -nE "bin/rl-|\./rl\b|\.venv/bin/python bin/rl" .claude README.md AGENTS.md SPEC.md`
Expected: no matches.

- [ ] **Step 2: Update `README.md` / `AGENTS.md`** — install (`just install-local` / `uv tool install .`), the `rl` command (drop `./rl`), the `~/.local/share/ringleader/` state dir, `--json`, and the `sources/` seam. Update AGENTS "Commands" + "Gotchas".

- [ ] **Step 3: Update `SPEC.md`** — the "Homes/layout" section (state now in home, `harvests/` gone, `sources/` added), and the CLI-crystallization note (now realized).

- [ ] **Step 4: Verify build + full gate**

Run: `uv build && just check`
Expected: a wheel builds; `just check` green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: update skills, README, AGENTS, SPEC for installed rl + state-in-home"
```

---

## Self-Review

**Spec coverage:** §1 packaging → T1+T2; §2 sources seam → T3; §3 `--json` → T5; §4 state/home/migrate/harvests → T4; §5 ruff (+optional mypy via `just typecheck`) → T6; §6 testing (pytest→pyproject in T1, cov config T1, test-invocation switch T2) → T1/T2; §7 Justfile → T7; §8 CI → T7; §9 ripples → T8. All sections covered.

**Placeholder scan:** no "TBD"/"add error handling"/"write tests for the above" — every code/config step carries real content; the 19-way port is a stated verbatim recipe + a full worked module (`commands/triage.py`), not an omission.

**Type consistency:** `NormalizedSession`/`RawRef`/`Source` (T3) are referenced with the same fields in the scanner rewire and source test; `state_dir`/`default_registry`/`archive_root` (T4) are used consistently in T4's command-default rewire and referenced ahead in `commands/triage.py` (T2 notes the temporary fallback until T4 lands); `cmd_<sub>` naming and the `commands.TABLE` keys match the dispatch in `cli.py`; `jsonout.triage_json`/`rollup_json` (T5) match their test.

**Ordering note:** Task 2's `commands/triage.py` imports `paths.default_registry`, which is created in Task 4. The task text flags this — use the old `REPO/registry.json` default in T2 and flip to `default_registry()` in T4 (T4 Step 6 does exactly that sweep). No other forward dependency.
