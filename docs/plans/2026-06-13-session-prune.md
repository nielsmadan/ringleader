# session-prune Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `./st prune` — bulk-delete tiny *and* stale dormant sessions (preview by default; `--delete` with a y/N confirm), removing each `.jsonl` + its `<uuid>/` sidecar dir and tombstoning it in the registry.

**Architecture:** A new pure module `sessiontracker/prune.py` selects candidates by age+size+turns; a new `registry.prune_session` mutator removes a thread and tombstones it; a thin `bin/st-prune` CLI does the preview/confirm/delete and reuses `projects.filter_by_projects` for `--project` scoping and `overview.{age_days,effective_status,_human_size}` for display.

**Tech Stack:** Python 3.10+ (stdlib only at runtime: `argparse`, `shutil`, `pathlib`, `datetime`), pytest (dev, in `.venv`).

## Global Constraints

- Runtime code is **stdlib-only**; pytest is dev-only, run as `.venv/bin/pytest`.
- Work on `main`; commit per task (approved for this repo).
- bin scripts resolve the repo via `Path(__file__).resolve().parent.parent` and `sys.path.insert(0, str(REPO))`, then `from sessiontracker import ...` (match existing `bin/st-*`).
- Timestamps are generated at the CLI edge (`datetime.now(timezone.utc).isoformat()`) and passed into pure functions.
- Candidate rule (from spec): `age_days ≥ older_than_days` AND (`sizeBytes < max_size_bytes` OR `messageCount < max_turns`), excluding `decision == "keep"`. Defaults: 7 days / 50 KB / 12 turns.

---

### Task 1: `prune.py` — candidate selection

**Files:**
- Create: `sessiontracker/prune.py`
- Test: `tests/test_prune.py`

**Interfaces:**
- Consumes: `sessiontracker.overview.age_days(mtime_iso, now_iso) -> float` (existing).
- Produces: `prune_candidates(reg: dict, now_iso: str, *, older_than_days: int = 7, max_size_bytes: int = 51200, max_turns: int = 12) -> list[dict]` and the module constants `DEFAULT_OLDER_THAN_DAYS = 7`, `DEFAULT_MAX_SIZE_BYTES = 51200`, `DEFAULT_MAX_TURNS = 12`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prune.py`:

```python
from sessiontracker.prune import (
    prune_candidates, DEFAULT_MAX_SIZE_BYTES, DEFAULT_MAX_TURNS,
)

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-23T00:00:00+00:00"        # exactly 7 days before NOW
RECENT = "2026-06-28T00:00:00+00:00"     # 2 days before NOW


def _t(sid, mtime=OLD, size=200_000, msgs=50, decision=None):
    return {"sessionId": sid, "mtime": mtime, "sizeBytes": size,
            "messageCount": msgs, "decision": decision, "status": "blocked",
            "title": None, "suggestedName": f"{sid}-name"}


def _reg(threads):
    return {"version": 1, "lastScan": NOW, "threads": threads, "tombstones": []}


def _ids(cands):
    return sorted(c["sessionId"] for c in cands)


def test_recent_is_never_a_candidate_even_if_tiny():
    reg = _reg([_t("a", mtime=RECENT, size=10, msgs=1)])
    assert prune_candidates(reg, NOW) == []


def test_age_boundary_exactly_threshold_is_included():
    # exactly 7 days old + tiny → included (>= threshold)
    reg = _reg([_t("a", mtime=OLD, size=10, msgs=1)])
    assert _ids(prune_candidates(reg, NOW)) == ["a"]


def test_small_by_size_or_by_turns_each_qualify():
    reg = _reg([
        _t("small_size", size=10_000, msgs=999),   # under size, many turns
        _t("few_turns", size=5_000_000, msgs=3),    # huge, but few turns
        _t("big", size=5_000_000, msgs=999),        # neither → excluded
    ])
    assert _ids(prune_candidates(reg, NOW)) == ["few_turns", "small_size"]


def test_thresholds_are_strict_less_than():
    reg = _reg([
        _t("at_size", size=DEFAULT_MAX_SIZE_BYTES, msgs=DEFAULT_MAX_TURNS),  # == both → not small
        _t("under_size", size=DEFAULT_MAX_SIZE_BYTES - 1, msgs=999),
    ])
    assert _ids(prune_candidates(reg, NOW)) == ["under_size"]


def test_keep_is_protected_other_decisions_eligible():
    reg = _reg([
        _t("keep", size=10, msgs=1, decision="keep"),
        _t("abandon", size=10, msgs=1, decision="abandon"),
        _t("harvested", size=10, msgs=1, decision="harvested"),
        _t("none", size=10, msgs=1, decision=None),
    ])
    assert _ids(prune_candidates(reg, NOW)) == ["abandon", "harvested", "none"]


def test_custom_thresholds():
    reg = _reg([_t("a", size=80_000, msgs=999)])      # not tiny under defaults
    assert prune_candidates(reg, NOW) == []
    got = prune_candidates(reg, NOW, max_size_bytes=100_000)
    assert _ids(got) == ["a"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_prune.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.prune'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/prune.py`:

```python
from __future__ import annotations

from . import overview

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_MAX_SIZE_BYTES = 50 * 1024
DEFAULT_MAX_TURNS = 12


def prune_candidates(reg: dict, now_iso: str, *,
                     older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
                     max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
                     max_turns: int = DEFAULT_MAX_TURNS) -> list[dict]:
    out = []
    for t in reg["threads"]:
        if t.get("decision") == "keep":
            continue
        if overview.age_days(t["mtime"], now_iso) < older_than_days:
            continue
        small = ((t.get("sizeBytes") or 0) < max_size_bytes
                 or (t.get("messageCount") or 0) < max_turns)
        if small:
            out.append(t)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_prune.py -q
```

Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/prune.py tests/test_prune.py
git commit -m "feat: prune candidate selection (tiny + stale)"
```

---

### Task 2: `registry.prune_session`

**Files:**
- Modify: `sessiontracker/registry.py`
- Test: `tests/test_registry.py` (append)

**Interfaces:**
- Produces: `prune_session(reg: dict, session_id: str, deleted_at: str, *, reason: str = "prune") -> dict` — removes the thread from `reg["threads"]` and appends a tombstone `{"sessionId", "deletedAt", "reason", "harvestPath"}` (preserving the thread's `harvestPath`). Idempotent: a second call for an already-tombstoned id adds no duplicate.

- [ ] **Step 1: Write the failing test (append to `tests/test_registry.py`)**

```python
from sessiontracker.registry import prune_session


def test_prune_session_removes_thread_and_tombstones():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    reg["threads"][0]["harvestPath"] = "harvests/s1.md"
    prune_session(reg, "s1", "2026-06-13T00:00:00+00:00")
    assert reg["threads"] == []
    assert len(reg["tombstones"]) == 1
    ts = reg["tombstones"][0]
    assert ts["sessionId"] == "s1"
    assert ts["deletedAt"] == "2026-06-13T00:00:00+00:00"
    assert ts["reason"] == "prune"
    assert ts["harvestPath"] == "harvests/s1.md"


def test_prune_session_is_idempotent():
    reg = reconcile(empty_registry(), [_meta("s1", NOW), _meta("s2", NOW)], NOW)
    prune_session(reg, "s1", NOW)
    prune_session(reg, "s1", NOW)        # second call: already gone + tombstoned
    assert [t["sessionId"] for t in reg["threads"]] == ["s2"]   # s2 untouched
    assert sum(1 for ts in reg["tombstones"] if ts["sessionId"] == "s1") == 1
```

(`reconcile`, `empty_registry`, `_meta`, `NOW` are already imported at the top of this file.)

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: FAIL — `ImportError: cannot import name 'prune_session'`.

- [ ] **Step 3: Append the implementation to `sessiontracker/registry.py`**

```python
def prune_session(reg: dict, session_id: str, deleted_at: str, *,
                  reason: str = "prune") -> dict:
    threads = reg["threads"]
    idx = next((i for i, t in enumerate(threads)
                if t["sessionId"] == session_id), None)
    harvest = threads[idx].get("harvestPath") if idx is not None else None
    if idx is not None:
        threads.pop(idx)
    tombstones = reg.setdefault("tombstones", [])
    if not any(ts["sessionId"] == session_id for ts in tombstones):
        tombstones.append({"sessionId": session_id, "deletedAt": deleted_at,
                           "reason": reason, "harvestPath": harvest})
    return reg
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: PASS (all registry tests green, including the 2 new ones).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/registry.py tests/test_registry.py
git commit -m "feat: registry prune_session (remove + tombstone)"
```

---

### Task 3: `bin/st-prune` CLI + dispatcher

**Files:**
- Create: `bin/st-prune`
- Modify: `st` (dispatcher usage text)
- Test: `tests/test_st_prune.py`

**Interfaces:**
- Consumes: `prune.prune_candidates(...)`, `registry.{load_registry,save_registry,prune_session}`, `projects.filter_by_projects(threads, selectors)`, `overview.{age_days,effective_status,_human_size}` (all existing/from Tasks 1–2).
- Produces: the `st prune` command. Behavior: preview by default; `--delete` prompts `Delete N sessions (SIZE)? [y/N] ` and, on `y`/`yes`, deletes each `<uuid>.jsonl` + `<uuid>/` sidecar dir and calls `prune_session`.

- [ ] **Step 1: Write the implementation**

Create `bin/st-prune`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry, prune, projects, overview  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(t: dict) -> str:
    return t.get("title") or t.get("suggestedName") or t["sessionId"][:8]


def main():
    ap = argparse.ArgumentParser(prog="st-prune")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--project", action="append", default=None,
                    help="repo/dir selector; repeatable — scope the prune")
    ap.add_argument("--older-than", type=int, default=prune.DEFAULT_OLDER_THAN_DAYS,
                    help="minimum age in days (default 7)")
    ap.add_argument("--max-size", type=int, default=prune.DEFAULT_MAX_SIZE_BYTES // 1024,
                    help="'tiny' size ceiling in KB (default 50)")
    ap.add_argument("--max-turns", type=int, default=prune.DEFAULT_MAX_TURNS,
                    help="'tiny' message-count ceiling (default 12)")
    ap.add_argument("--delete", action="store_true",
                    help="actually delete (otherwise preview only)")
    args = ap.parse_args()

    now = _now()
    reg = registry.load_registry(args.registry)
    cands = prune.prune_candidates(
        reg, now, older_than_days=args.older_than,
        max_size_bytes=args.max_size * 1024, max_turns=args.max_turns)
    if args.project:
        cands = projects.filter_by_projects(cands, args.project)

    if not cands:
        print("nothing to prune")
        return

    cands.sort(key=lambda t: overview.age_days(t["mtime"], now), reverse=True)
    total = sum(t.get("sizeBytes") or 0 for t in cands)
    for t in cands:
        age = round(overview.age_days(t["mtime"], now))
        print(f"  [{overview.effective_status(t, now)}] {_label(t)} "
              f"({age}d, {overview._human_size(t.get('sizeBytes'))}, "
              f"{t.get('messageCount') or 0} msgs)  {t.get('project') or ''}")
    print(f"\n{len(cands)} sessions, {overview._human_size(total)}")

    if not args.delete:
        print("(preview — re-run with --delete to remove these)")
        return

    try:
        resp = input(f"Delete {len(cands)} sessions "
                     f"({overview._human_size(total)})? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    if resp not in ("y", "yes"):
        print("aborted, nothing deleted")
        return

    deleted = skipped = 0
    for t in cands:
        f = Path(t["file"])
        sidecar = f.with_suffix("")        # <uuid>.jsonl -> <uuid>/ dir
        try:
            f.unlink(missing_ok=True)
            shutil.rmtree(sidecar, ignore_errors=True)
        except OSError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.prune_session(reg, t["sessionId"], now)
        deleted += 1
    registry.save_registry(args.registry, reg)
    print(f"deleted {deleted} sessions, skipped {skipped}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable + add the dispatcher usage line**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-prune
```

In `st`, add a usage line after the `st project` line so it reads:

```
  st project  <selector> [--all]   DEEP report for one repo (across checkouts)
  st prune    [--project <sel>] [--delete]   delete tiny + stale sessions (preview by default)
  st overview [--project <sel>] [--all]   live threads (--all = every status)
```

- [ ] **Step 3: Write the subprocess integration test**

Create `tests/test_st_prune.py`:

```python
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"


def _thread(tmp, sid, *, size_bytes, msgs=1, mtime="2026-01-01T00:00:00+00:00",
            decision=None, git_label="repo", project="/p"):
    """Write a real transcript file + sidecar dir, return the registry thread."""
    f = tmp / f"{sid}.jsonl"
    f.write_text("x" * size_bytes, encoding="utf-8")
    side = tmp / sid
    side.mkdir()
    (side / "note.txt").write_text("sidecar", encoding="utf-8")
    return {
        "sessionId": sid, "file": str(f), "project": project,
        "title": None, "aiTitle": None, "summary": None, "lastPrompt": "lp",
        "mtime": mtime, "sizeBytes": size_bytes, "messageCount": msgs,
        "gitBranch": None, "gitProjectKey": git_label + "-k",
        "gitProjectLabel": git_label, "gitRoot": None, "gitRemote": None,
        "status": "blocked", "statusConfidence": 0.5, "whatItIs": "w",
        "suggestedName": f"{sid}-name", "appliedName": None, "namedAtMtime": None,
        "classifiedAtMtime": None, "decision": decision, "harvestPath": None,
        "todoId": None, "tags": [],
    }


def _write_registry(tmp, threads):
    p = tmp / "registry.json"
    p.write_text(json.dumps({"version": 1, "lastScan": "2026-06-30T00:00:00+00:00",
                             "threads": threads, "tombstones": []}), encoding="utf-8")
    return p


def _run(reg, *args, stdin=""):
    return subprocess.run(
        [str(PY), str(REPO / "bin" / "st-prune"), "--registry", str(reg), *args],
        cwd=str(REPO), capture_output=True, text=True, input=stdin)


def test_preview_deletes_nothing(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    r = _run(reg)
    assert r.returncode == 0
    assert "a-name" in r.stdout and "preview" in r.stdout
    assert Path(t["file"]).exists()                 # nothing deleted
    assert json.loads(reg.read_text())["threads"]   # registry unchanged


def test_delete_yes_removes_file_sidecar_and_tombstones(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="y\n")
    assert r.returncode == 0 and "deleted 1" in r.stdout
    assert not Path(t["file"]).exists()             # transcript gone
    assert not (tmp_path / "a").exists()            # sidecar dir gone
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    assert data["tombstones"][0]["sessionId"] == "a"
    assert data["tombstones"][0]["reason"] == "prune"


def test_delete_no_keeps_everything(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="n\n")
    assert "aborted" in r.stdout
    assert Path(t["file"]).exists()
    assert json.loads(reg.read_text())["threads"]


def test_project_scope_limits_candidates(tmp_path):
    a = _thread(tmp_path, "a", size_bytes=100, git_label="repo-a")
    b = _thread(tmp_path, "b", size_bytes=100, git_label="repo-b")
    reg = _write_registry(tmp_path, [a, b])
    r = _run(reg, "--project", "repo-a", "--delete", stdin="y\n")
    assert "deleted 1" in r.stdout
    assert not Path(a["file"]).exists()
    assert Path(b["file"]).exists()                 # repo-b untouched


def test_missing_transcript_is_tolerated(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100)
    Path(t["file"]).unlink()                        # gone out-of-band
    reg = _write_registry(tmp_path, [t])
    r = _run(reg, "--delete", stdin="y\n")
    assert r.returncode == 0 and "deleted 1" in r.stdout
    assert json.loads(reg.read_text())["tombstones"][0]["sessionId"] == "a"


def test_keep_marked_session_never_listed(tmp_path):
    t = _thread(tmp_path, "a", size_bytes=100, decision="keep")
    reg = _write_registry(tmp_path, [t])
    r = _run(reg)
    assert "nothing to prune" in r.stdout
    assert Path(t["file"]).exists()
```

- [ ] **Step 4: Run the integration test**

```bash
cd ~/wrksp/incubation/session-tracker
.venv/bin/pytest tests/test_st_prune.py -q
```

Expected: PASS (6 passed).

- [ ] **Step 5: Run the full suite + a real-data preview sanity check**

```bash
.venv/bin/pytest -q
.venv/bin/python bin/st-prune | tail -5          # real registry, PREVIEW ONLY (no --delete)
```

Expected: suite all green; the preview lists real tiny+old sessions and a total, ending with the "(preview — …)" hint — and deletes nothing.

- [ ] **Step 6: Commit**

```bash
git add bin/st-prune st tests/test_st_prune.py
git commit -m "feat: st prune CLI (preview + confirmed delete)"
```

---

## Self-review against the spec

- **Candidate rule** (age ≥ N AND (size< OR turns<), `keep`-protected, status-agnostic): Task 1 `prune_candidates` + tests for each axis, boundaries, decisions. ✓
- **Defaults 7d / 50KB / 12turns:** Task 1 constants; Task 3 flags default to them (`--max-size` in KB → ×1024). ✓
- **Preview by default; `--delete` + y/N:** Task 3 CLI + tests (`preview_deletes_nothing`, `delete_yes…`, `delete_no…`). ✓
- **Delete `.jsonl` + `<uuid>/` sidecar dir:** Task 3 `f.unlink` + `rmtree(f.with_suffix(""))` + test asserts both gone. ✓
- **Tombstone with `reason="prune"` + preserved harvestPath; immediate registry update:** Task 2 `prune_session` + tests. ✓
- **`--project` scoping:** Task 3 via `filter_by_projects` + test. ✓
- **Edge cases** (missing file tolerated, empty set → "nothing to prune", keep excluded, OSError skip): Task 3 tests cover missing-file, keep; empty-set and EOF/OSError handled in code. ✓
- **`./st prune` dispatcher:** Task 3 Step 2 (generic passthrough already routes; usage line added). ✓
- **Non-goals (harvest/archive/triage):** none added. ✓
