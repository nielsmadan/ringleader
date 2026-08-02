# CLI API Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Regroup ringleader's 20 flat top-level commands into 3 flat daily verbs plus `session`/`mem`/`data`/`config` namespaces, updating every consumer (skills, docs, tests).

**Architecture:** `cli.py` gains a nested-dispatch walker over a nested `commands.TABLE`; existing `cmd_*` functions are untouched and only re-wired. Work is staged additive-first (Task 1 adds the new surface while the old flat names keep working) then hard-cut (Task 3 removes the old names and the `--memory` flag). This keeps every task's test suite green.

**Tech Stack:** Python ≥3.13, stdlib `argparse`, `pytest` (via `uv run pytest`), `ruff`.

## Global Constraints

- **Pure rename/regroup.** No command changes *what it does*; only its name/path changes. No behavioral assertions change.
- **Final state is a hard cut, no aliases.** Old flat names (`rl sync`, `rl archive`, `rl mem-scan`, …) must be unknown commands (exit 2) after Task 3.
- **Two nesting levels max.** Deepest path is `rl session mark harvested`.
- **Exact final surface:** flat `overview` `triage` `prune`; `rl session <refresh|set|enrich|archive|delete|complete|snooze|wake|name|relocate|mark …>`; `rl mem <refresh|prune|triage|overview|mark …>`; `rl data <export|tokens|migrate>`; `rl config` (reserved). (`sync scan`→`session refresh`, `sync apply`→`session set`, `mem scan`→`mem refresh`.)
- **Reserved verbs** (`session enrich`, `session complete`, `config`): listed in help as `(coming soon)`; if invoked, print `not yet implemented — see Spec 2` to stderr and return 2.
- **`--memory` is removed from the public surface.** That scope lives only in `rl mem triage` / `rl mem overview` after Task 3.
- **No new runtime dependencies.** `dependencies` in `pyproject.toml` stays as-is.
- Tests must pass with `uv run pytest -q`; `uv run ruff check` and `uv run ruff format --check` must be clean.

## File Structure

- `ringleader/cli.py` — **modify.** Replace the single-level dispatch with a walker that descends nested group tables (up to 3 tokens) and renders grouped help / group usage.
- `ringleader/commands/__init__.py` — **modify.** Reshape `TABLE` from flat `{name: fn}` to nested `{name: fn | {sub: fn | {sub2: fn}}}`; add reserved stubs.
- `ringleader/commands/triage.py` — **modify.** Extract the memory-inbox branch into `cmd_mem_triage`; `cmd_triage` calls the shared helper (Task 1), then loses `--memory` (Task 3).
- `ringleader/commands/overview.py` — **modify.** Same extraction into `cmd_mem_overview`.
- `tests/test_rl_dispatch.py` — **modify.** New routing / group-usage / reserved-verb / old-name-unknown tests.
- `tests/test_rl_curation.py`, `test_rl_export.py`, `test_rl_mem.py`, `test_rl_name.py`, `test_rl_overview.py`, `test_rl_prune.py`, `test_rl_triage.py` — **modify.** Update `_run(...)` argv to new paths.
- `.claude/skills/*/SKILL.md` (6 files) — **modify.** Update `rl` call-sites and prose.
- `README.md`, `AGENTS.md`, `Justfile` — **modify.** Update command surface and the `rl migrate` call.

## Reference: current invocation styles

- Most `tests/test_rl_*.py` shell out via a local helper `_run(sub, *args)` →
  `subprocess.run([sys.executable, "-m", "ringleader", sub, *args])`. To reach a
  grouped command, insert the group token as the first arg: `_run("archive", ...)` →
  `_run("session", "archive", ...)`; `_run("mem-scan", ...)` → `_run("mem", "scan", ...)`.
- `tests/test_rl_dispatch.py` calls `from ringleader.cli import main` directly with an
  argv list: `main(["overview"])`.
- `main(argv)` returns an int exit code and each `cmd_*(argv)` parses its own residual
  args with its own `argparse.ArgumentParser`.

---

### Task 1: Nested dispatch + additive new surface

Add the full new command surface (groups + reserved stubs) while keeping every old flat name working, so the existing suite stays green and only new behavior is tested here.

**Files:**
- Modify: `ringleader/cli.py`
- Modify: `ringleader/commands/__init__.py`
- Modify: `ringleader/commands/triage.py`
- Modify: `ringleader/commands/overview.py`
- Test: `tests/test_rl_dispatch.py`

**Interfaces:**
- Produces:
  - `ringleader.commands.TABLE: dict[str, Cmd | dict]` where `Cmd = Callable[[list[str]], int]`; values are either a leaf command or a subgroup dict (nested up to one more level for `mark`).
  - `ringleader.cli.main(argv: list[str] | None = None) -> int` — walks `TABLE` token by token: descends into dict entries, calls the first callable it reaches with the remaining argv, returns 2 on an unknown token, prints group usage and returns 0 when argv is exhausted at a group node.
  - `ringleader.commands.triage.cmd_mem_triage(argv: list[str]) -> int` and `ringleader.commands.overview.cmd_mem_overview(argv: list[str]) -> int` — the memory-inbox / memory-rollup entry points, sharing the existing implementation with the `--memory` branch.
  - A reserved-stub factory in `ringleader/commands/__init__.py`: `_reserved(name: str) -> Cmd` returning a callable that prints `rl {name}: not yet implemented — see Spec 2` to stderr and returns 2.

- [ ] **Step 1: Write failing dispatch tests**

Add to `tests/test_rl_dispatch.py`:

```python
import pytest

from ringleader.cli import main
from ringleader import commands


@pytest.mark.parametrize(
    "path,expected",
    [
        (["session", "archive"], commands.archive.cmd_archive),
        (["session", "delete"], commands.delete.cmd_delete),
        (["session", "snooze"], commands.snooze.cmd_snooze),
        (["session", "wake"], commands.wake.cmd_wake),
        (["session", "scan"], commands.sync.cmd_sync),
        (["session", "name"], commands.name.cmd_name),
        (["session", "relocate"], commands.relocate.cmd_relocate),
        (["session", "mark", "harvested"], commands.mark_harvested.cmd_mark_harvested),
        (["session", "mark", "reviewed"], commands.mark_reviewed.cmd_mark_reviewed),
        (["session", "mark", "cleaned"], commands.cleaned.cmd_cleaned),
        (["mem", "scan"], commands.mem_scan.cmd_mem_scan),
        (["mem", "prune"], commands.mem_prune.cmd_mem_prune),
        (["mem", "mark", "cleaned"], commands.mem_cleaned.cmd_mem_cleaned),
        (["mem", "mark", "reviewed"], commands.mark_mem_reviewed.cmd_mark_mem_reviewed),
        (["data", "export"], commands.export.cmd_export),
        (["data", "tokens"], commands.tokens.cmd_tokens),
        (["data", "migrate"], commands.migrate.cmd_migrate),
    ],
)
def test_new_paths_resolve(path, expected):
    node = commands.TABLE
    for tok in path:
        node = node[tok]
    assert node is expected


def test_group_with_no_subcommand_lists_subs(capsys):
    rc = main(["session"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "archive" in out and "snooze" in out


def test_unknown_subcommand_returns_2(capsys):
    rc = main(["session", "bogus"])
    assert rc == 2
    assert "bogus" in capsys.readouterr().err


@pytest.mark.parametrize("path", [["session", "enrich"], ["session", "complete"], ["config"]])
def test_reserved_verbs_return_2(path, capsys):
    rc = main(path)
    assert rc == 2
    assert "not yet implemented" in capsys.readouterr().err


def test_help_lists_groups(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "session" in out and "mem" in out and "data" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rl_dispatch.py -q`
Expected: FAIL (`KeyError`/`TypeError` on the nested lookups; `session`/reserved not routed).

- [ ] **Step 3: Extract the memory entry points**

In `ringleader/commands/triage.py`, factor the body currently under `if args.memory:` into a module-level helper and a public entry point, leaving `cmd_triage` calling the helper (so `--memory` still works this task):

```python
def _memory_inbox(registry_path: str, projects, no_scan: bool) -> int:
    # ...the exact code that was inside `if args.memory:`, using these params
    # in place of args.registry / args.project / args.no_scan...
    ...


def cmd_mem_triage(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl mem triage")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--no-scan", action="store_true")
    a = ap.parse_args(argv)
    return _memory_inbox(a.registry, a.project, a.no_scan)
```

Inside `cmd_triage`, replace the memory branch body with:
`if args.memory: return _memory_inbox(args.registry, args.project, args.no_scan)`.

Do the equivalent extraction in `ringleader/commands/overview.py`: `_memory_rollup(...)` helper + `cmd_mem_overview(argv)` public entry, and `cmd_overview`'s `if args.memory:` branch calls it. Preserve the exact existing memory output.

- [ ] **Step 4: Add the reserved-stub factory and reshape TABLE**

In `ringleader/commands/__init__.py`, after the imports add:

```python
import sys as _sys


def _reserved(name):
    def _stub(argv):
        print(f"rl {name}: not yet implemented — see Spec 2", file=_sys.stderr)
        return 2

    return _stub
```

Replace `TABLE` with the nested structure (old flat entries kept alongside for this task):

```python
_SESSION = {
    "scan": sync.cmd_sync,
    "enrich": _reserved("session enrich"),
    "archive": archive.cmd_archive,
    "delete": delete.cmd_delete,
    "complete": _reserved("session complete"),
    "snooze": snooze.cmd_snooze,
    "wake": wake.cmd_wake,
    "name": name.cmd_name,
    "relocate": relocate.cmd_relocate,
    "mark": {
        "harvested": mark_harvested.cmd_mark_harvested,
        "reviewed": mark_reviewed.cmd_mark_reviewed,
        "cleaned": cleaned.cmd_cleaned,
    },
}
_MEM = {
    "scan": mem_scan.cmd_mem_scan,
    "prune": mem_prune.cmd_mem_prune,
    "triage": triage.cmd_mem_triage,
    "overview": overview.cmd_mem_overview,
    "mark": {
        "cleaned": mem_cleaned.cmd_mem_cleaned,
        "reviewed": mark_mem_reviewed.cmd_mark_mem_reviewed,
    },
}
_DATA = {
    "export": export.cmd_export,
    "tokens": tokens.cmd_tokens,
    "migrate": migrate.cmd_migrate,
}

TABLE = {
    # flat daily verbs
    "overview": overview.cmd_overview,
    "triage": triage.cmd_triage,
    "prune": prune.cmd_prune,
    # namespaces
    "session": _SESSION,
    "mem": _MEM,
    "data": _DATA,
    "config": _reserved("config"),
    # --- old flat names (removed in Task 3) ---
    "sync": sync.cmd_sync,
    "archive": archive.cmd_archive,
    "delete": delete.cmd_delete,
    "snooze": snooze.cmd_snooze,
    "wake": wake.cmd_wake,
    "name": name.cmd_name,
    "relocate": relocate.cmd_relocate,
    "mark-harvested": mark_harvested.cmd_mark_harvested,
    "mark-reviewed": mark_reviewed.cmd_mark_reviewed,
    "cleaned": cleaned.cmd_cleaned,
    "mem-scan": mem_scan.cmd_mem_scan,
    "mem-prune": mem_prune.cmd_mem_prune,
    "mem-cleaned": mem_cleaned.cmd_mem_cleaned,
    "mark-mem-reviewed": mark_mem_reviewed.cmd_mark_mem_reviewed,
    "export": export.cmd_export,
    "tokens": tokens.cmd_tokens,
    "migrate": migrate.cmd_migrate,
}
```

- [ ] **Step 5: Rewrite the dispatch walker + help in cli.py**

Replace `ringleader/cli.py` with a walker that descends group dicts. Help lists the flat verbs and the namespaces (with sub-command names); reserved entries get a `(coming soon)` marker:

```python
from __future__ import annotations

import sys

from . import commands

TABLE = commands.TABLE

_FLAT = ("overview", "triage", "prune")
_GROUPS = ("session", "mem", "data", "config")
_RESERVED = {"session enrich", "session complete", "config"}


def _leaf_names(node) -> str:
    return "  ".join(k for k in node) if isinstance(node, dict) else ""


def _usage(stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    print("ringleader — usage: rl <command> [args]\n", file=stream)
    print("commands: " + "  ".join(_FLAT), file=stream)
    for g in _GROUPS:
        node = TABLE.get(g)
        subs = _leaf_names(node) if isinstance(node, dict) else "(coming soon)"
        print(f"  {g}: {subs}", file=stream)


def _group_usage(crumbs: list[str], node: dict, stream) -> None:
    print(f"rl {' '.join(crumbs)} — subcommands: " + _leaf_names(node), file=stream)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0
    node = TABLE
    crumbs: list[str] = []
    rest = argv
    while rest:
        tok, rest = rest[0], rest[1:]
        entry = node.get(tok) if isinstance(node, dict) else None
        if entry is None:
            where = f" in 'rl {' '.join(crumbs)}'" if crumbs else ""
            print(f"rl: unknown command '{tok}'{where}", file=sys.stderr)
            _usage(sys.stderr)
            return 2
        crumbs.append(tok)
        if callable(entry):
            return entry(rest) or 0
        node = entry
    _group_usage(crumbs, node, sys.stdout)
    return 0
```

- [ ] **Step 6: Run the dispatch tests**

Run: `uv run pytest tests/test_rl_dispatch.py -q`
Expected: PASS.

- [ ] **Step 7: Run the full suite (old flat names still work)**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check`
Expected: PASS — the existing `test_rl_*` tests still use old flat names, which remain in `TABLE`.

- [ ] **Step 8: Commit**

```bash
git add ringleader/cli.py ringleader/commands/__init__.py ringleader/commands/triage.py ringleader/commands/overview.py tests/test_rl_dispatch.py
git commit -m "feat: add grouped rl command surface alongside the flat one"
```

---

### Task 2: Migrate consumers (tests + skills) to the new surface

Point every consumer at the new paths. Old flat names still resolve, so this stays green; Task 3 then removes them.

**Files:**
- Modify: `tests/test_rl_curation.py`, `tests/test_rl_export.py`, `tests/test_rl_mem.py`, `tests/test_rl_name.py`, `tests/test_rl_overview.py`, `tests/test_rl_prune.py`, `tests/test_rl_triage.py`
- Modify: `.claude/skills/session-overview/SKILL.md`, `.claude/skills/session-sync/SKILL.md`, `.claude/skills/session-name/SKILL.md`, `.claude/skills/session-triage/SKILL.md`, `.claude/skills/session-harvest/SKILL.md`, `.claude/skills/memory-triage/SKILL.md`

**Interfaces:**
- Consumes: the new paths from Task 1 (`session <sub>`, `mem <sub>`, `data <sub>`).

- [ ] **Step 1: Update test argv to new paths**

In each `tests/test_rl_*.py`, rewrite `_run(...)` calls using the Task-1 mapping. Examples (apply to every occurrence):

- `_run("archive", ...)` → `_run("session", "archive", ...)`
- `_run("delete", ...)` → `_run("session", "delete", ...)`
- `_run("snooze"/"wake"/"relocate"/"name", ...)` → `_run("session", "<that>"...)`
- `_run("sync", "scan", ...)` → `_run("session", "refresh", ...)`; `_run("sync", "apply", ...)` → `_run("session", "set", ...)`
- `_run("mark-harvested"/"mark-reviewed"/"cleaned", ...)` → `_run("session", "mark", "<harvested|reviewed|cleaned>", ...)`
- `_run("mem-scan", ...)` → `_run("mem", "refresh", ...)`; `_run("mem-prune", ...)` → `_run("mem", "prune", ...)`
- `_run("mem-cleaned"/"mark-mem-reviewed", ...)` → `_run("mem", "mark", "cleaned"/"reviewed", ...)`
- `_run("export"/"tokens"/"migrate", ...)` → `_run("data", "<that>", ...)`
- `_run("triage", "--memory", ...)` → `_run("mem", "triage", ...)`
- `_run("overview", "--memory", ...)` → `_run("mem", "overview", ...)`

- [ ] **Step 2: Run the migrated test files**

Run: `uv run pytest tests/test_rl_curation.py tests/test_rl_export.py tests/test_rl_mem.py tests/test_rl_name.py tests/test_rl_overview.py tests/test_rl_prune.py tests/test_rl_triage.py -q`
Expected: PASS (new paths resolve via Task 1).

- [ ] **Step 3: Update the six skills**

In each `SKILL.md`, replace every `rl <old>` with its new path (same mapping as Step 1) in both command examples and prose. Notable spots: `session-sync`/`session-overview` use `rl sync scan`/`rl sync apply` → `rl session refresh`/`rl session set`; `session-name` uses `rl name [--project]` → `rl session name`; `session-triage` uses `rl archive`/`rl delete`/`rl mark-*`/`rl cleaned`/`rl export`; `session-harvest` uses `rl mark-harvested`/`rl export`; `memory-triage` uses `rl triage --memory`/`rl mem-scan`/`rl mem-prune`/`rl mem-cleaned`/`rl mark-mem-reviewed`/`rl overview --memory`.

- [ ] **Step 4: Verify no old names remain in skills**

Run:
```bash
grep -rnE "rl (sync|archive|delete|snooze|wake|name|relocate|mark-harvested|mark-reviewed|cleaned|mem-scan|mem-prune|mem-cleaned|mark-mem-reviewed|export|tokens|migrate)\b|rl (triage|overview) --memory" .claude/skills/
```
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add tests/test_rl_curation.py tests/test_rl_export.py tests/test_rl_mem.py tests/test_rl_name.py tests/test_rl_overview.py tests/test_rl_prune.py tests/test_rl_triage.py .claude/skills
git commit -m "chore: point tests and skills at the grouped rl surface"
```

---

### Task 3: Hard cut — remove old names, drop `--memory`, update docs

Delete the transitional flat entries and the `--memory` flag, finalize help, and update the docs/Justfile. After this the old names are unknown commands.

**Files:**
- Modify: `ringleader/commands/__init__.py` (remove old flat entries)
- Modify: `ringleader/commands/triage.py`, `ringleader/commands/overview.py` (remove `--memory`)
- Modify: `tests/test_rl_dispatch.py` (assert old names/`--memory` are gone)
- Modify: `README.md`, `AGENTS.md`, `Justfile`

**Interfaces:**
- Consumes: the extracted `cmd_mem_triage`/`cmd_mem_overview` from Task 1 (they no longer depend on the `--memory` flag).

- [ ] **Step 1: Write failing hard-cut tests**

Add to `tests/test_rl_dispatch.py`:

```python
@pytest.mark.parametrize(
    "name",
    [
        "sync", "archive", "delete", "snooze", "wake", "name", "relocate",
        "mark-harvested", "mark-reviewed", "cleaned",
        "mem-scan", "mem-prune", "mem-cleaned", "mark-mem-reviewed",
        "export", "tokens", "migrate",
    ],
)
def test_old_flat_names_are_unknown(name, capsys):
    rc = main([name])
    assert rc == 2
    assert "unknown command" in capsys.readouterr().err


def test_triage_memory_flag_removed():
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "ringleader", "triage", "--memory"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "unrecognized arguments" in r.stderr or "--memory" in r.stderr
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_rl_dispatch.py -q`
Expected: FAIL (old names still resolve; `--memory` still accepted).

- [ ] **Step 3: Remove the transitional flat entries**

In `ringleader/commands/__init__.py`, delete every entry under the `# --- old flat names (removed in Task 3) ---` comment, leaving only `overview`, `triage`, `prune`, `session`, `mem`, `data`, `config`.

- [ ] **Step 4: Remove the `--memory` flag from the session verbs**

In `ringleader/commands/triage.py`: delete the `--memory`/`-m` `add_argument`, the `if args.memory and args.json:` guard, and the `if args.memory:` branch (its body now lives in `_memory_inbox`, reached only via `cmd_mem_triage`). In `ringleader/commands/overview.py`: delete the `--memory` `add_argument` and the `if args.memory:` branch (kept in `cmd_mem_overview`). Adjust any now-stale `--json` help text that referenced `--memory`.

- [ ] **Step 5: Run the dispatch + full suite**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check`
Expected: PASS.

- [ ] **Step 6: Update README, AGENTS.md, Justfile**

- `README.md`: rewrite the command surface — `## Everyday` keeps `overview`/`triage`/`prune`; replace the `## Plumbing` list and every `rl <old>` mention with the grouped paths; drop the `--memory` flag docs on `overview`/`triage` and document `rl mem triage` / `rl mem overview` instead.
- `AGENTS.md`: update the `## Commands` block and any `rl <old>` references.
- `Justfile`: line 29 `uv run rl migrate` → `uv run rl data migrate`.

- [ ] **Step 7: Verify no old names remain anywhere tracked**

Run:
```bash
grep -rnE "rl (sync|archive|delete|snooze|wake|name|relocate|mark-harvested|mark-reviewed|cleaned|mem-scan|mem-prune|mem-cleaned|mark-mem-reviewed|export|tokens|migrate)\b|rl (triage|overview) --memory" README.md AGENTS.md Justfile .claude/skills/ docs/specs/2026-08-02-api-redesign-design.md
```
Expected: no output (the spec doc's old→new mapping table uses backticks without a leading `rl `, so it won't match; if it does, it is the historical mapping and may stay).

- [ ] **Step 8: Commit**

```bash
git add ringleader/commands/__init__.py ringleader/commands/triage.py ringleader/commands/overview.py tests/test_rl_dispatch.py README.md AGENTS.md Justfile
git commit -m "feat: cut over to the grouped rl command surface"
```

---

## Self-Review

**Spec coverage:**
- Target command surface → Task 1 (`TABLE` shape) + Task 3 (hard cut). ✓
- Old→new mapping (all 20 + 3 reserved) → Task 1 table, Task 2 consumer migration, Task 3 removal. ✓
- Hard cut, no aliases → Task 3 Steps 3 + 1-test. ✓
- Dispatch mechanics (≤2 nesting, nested-table walker) → Task 1 Step 5. ✓
- Help / group usage / reserved markers → Task 1 Step 5 + tests. ✓
- `--memory` removal → Task 1 extraction + Task 3 Step 4. ✓
- Skill call-site updates → Task 2 Steps 3-4. ✓
- Testing (routing, group usage, reserved, behavior preserved, skill-lint) → Task 1 + Task 2 Step 4 + Task 3. ✓
- README/AGENTS/Justfile → Task 3 Step 6. ✓
- Reserved verbs (enrich/complete/config) exit 2 with message → Task 1 `_reserved` + test. ✓

**Placeholder scan:** No TBD/TODO; every code step has concrete code; the memory-branch extraction references the exact existing branches by their `if args.memory:` location.

**Type consistency:** `Cmd = Callable[[list[str]], int]`; `TABLE` values are `Cmd | dict`; `main` and every `cmd_*`/`cmd_mem_*` return `int`. `sync.cmd_sync` maps to the `scan` path (function name unchanged, only its path renamed) — consistent across Tasks 1-3.

## Out of scope

- LLM behavior for `enrich`/`complete`/`config`, the Model/Target seams, settings — Spec 2.
- The interactive TUI — Spec 3.
- Any change to what a command does — this is a pure rename/regroup.
