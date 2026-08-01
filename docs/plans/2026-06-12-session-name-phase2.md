# session-name (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `session-name` — apply the registry's `suggestedName`s to dormant Claude Code sessions by appending a `custom-title` record to each transcript (naming new ones and re-naming ones whose focus drifted), scoped per repo/dir via `--project`, plus a `st-projects` listing view.

**Architecture:** Two new stdlib-only pure modules in the existing `sessiontracker/` package — `naming.py` (eligibility + name generation) and `projects.py` (selector matching + summary) — both fully TDD'd. Two new thin CLIs (`bin/st-name`, `bin/st-projects`) and a small `--project` filter added to `bin/st-sync scan`. The registry gains provenance fields (`appliedName`, `namedAtMtime`) so re-naming only ever touches names we generated. Two skills wire it up conversationally.

**Tech Stack:** Python 3.10+ (stdlib only at runtime), pytest (dev, in `.venv`), Markdown SKILL.md files.

---

## Design decisions locked from the spec

- **Done-detection is `session-sync`'s job, not this slice.** `session-name` only applies/refreshes names from `suggestedName`.
- **Provenance:** `st-name` records `appliedName` (last title *we* wrote) and `namedAtMtime`. Re-naming only fires for threads whose current `title == appliedName` and whose fresh `suggestedName` now differs. User-set names (`title != appliedName`) are never touched.
- **Mechanism:** append `{"type":"custom-title","customTitle":"<name>","sessionId":"<uuid>"}` at EOF (last-wins). A rename is just another append — never a file rewrite.
- **Scoping:** `--project <selector>` (repeatable) matches a git project label/key/root or a cwd path (equality, or path-prefix on root/cwd), applied to both `st-name` and `st-sync scan`.
- **Collisions:** when a generated name would collide with an existing/assigned title, append a 6-hex-char suffix from the sessionId.
- **Active-session guard:** skip files modified within `active_window_minutes` (default 10).
- **No backups / no undo / auto-apply** (with `--dry-run` for preview).

## File structure

```
session-tracker/
├── sessiontracker/
│   ├── registry.py        Task 1   add appliedName/namedAtMtime + apply_name()
│   ├── projects.py        Task 2,3 NEW: matches / filter_by_projects / project_summary
│   ├── naming.py          Task 4,5,6 NEW: title_line / unique_name / candidate_kind / select_eligible
│   ├── overview.py        (reused: age_days, group_key, group_label)
│   └── scanner.py          (unchanged)
├── tests/
│   ├── test_registry.py   Task 1   (append)
│   ├── test_projects.py   Task 2,3 NEW
│   └── test_naming.py     Task 4,5,6 NEW
├── bin/
│   ├── st-name            Task 7   NEW
│   ├── st-projects        Task 8   NEW
│   └── st-sync            Task 9   add --project to scan
└── .claude/skills/
    ├── session-name/SKILL.md       Task 10 NEW
    └── session-overview/SKILL.md   Task 11 extend
```

All work is on `main` (per the Phase-1 decision). Use the existing venv: `.venv/bin/pytest`.

---

### Task 1: registry — name provenance + `apply_name`

Add `appliedName` / `namedAtMtime` to new threads (default `null`) and a registry mutator `apply_name`. `reconcile` already copy-on-writes and only overwrites metadata/git fields, so these new fields are preserved across rescans for free — we add a test to prove it.

**Files:**
- Modify: `sessiontracker/registry.py`
- Test: `tests/test_registry.py` (append)

- [ ] **Step 1: Write the failing test (append to `tests/test_registry.py`)**

```python
from sessiontracker.registry import apply_name


def test_new_thread_has_null_name_provenance():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    t = reg["threads"][0]
    assert t["appliedName"] is None
    assert t["namedAtMtime"] is None


def test_apply_name_sets_title_and_provenance():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    apply_name(reg, "s1", "login-button")
    t = reg["threads"][0]
    assert t["title"] == "login-button"
    assert t["appliedName"] == "login-button"
    assert t["namedAtMtime"] == t["mtime"]


def test_apply_name_unknown_session_raises():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    with pytest.raises(KeyError):
        apply_name(reg, "nope", "x")


def test_reconcile_preserves_name_provenance():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    apply_name(reg, "s1", "thing-x")
    # rescan with a newer mtime → metadata refreshes, provenance preserved
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    t = reg2["threads"][0]
    assert t["mtime"] == "2026-06-05T00:00:00+00:00"
    assert t["appliedName"] == "thing-x"
    assert t["namedAtMtime"] == "2026-06-01T00:00:00+00:00"
```

(`_meta`, `reconcile`, `empty_registry`, `NOW`, and `pytest` are already imported at the top of this file from Phase 1.)

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: FAIL — `ImportError: cannot import name 'apply_name'`.

- [ ] **Step 3: Add the fields to `_new_thread` and the `apply_name` function**

In `sessiontracker/registry.py`, inside `_new_thread`, add two keys right after the `"tags": [],` line is fine, but place them next to the other name fields — insert after `"suggestedName": None,`:

```python
        "suggestedName": None,
        "appliedName": None,
        "namedAtMtime": None,
```

Then append this function at the end of the file:

```python
def apply_name(reg: dict, session_id: str, name: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["title"] = name
            t["appliedName"] = name
            t["namedAtMtime"] = t["mtime"]
            return reg
    raise KeyError(session_id)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: PASS (all registry tests green, including the 4 new ones).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/registry.py tests/test_registry.py
git commit -m "feat: registry name provenance and apply_name"
```

---

### Task 2: `projects.py` — selector matching + filtering

**Files:**
- Create: `sessiontracker/projects.py`
- Test: `tests/test_projects.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_projects.py`:

```python
from sessiontracker.projects import matches, filter_by_projects


def _thread(sid="s1", project="/r/repo/sub", git_label="repo",
            git_key="github.com/me/repo", git_root="/r/repo"):
    return {
        "sessionId": sid, "project": project,
        "gitProjectLabel": git_label, "gitProjectKey": git_key,
        "gitRoot": git_root,
    }


def test_matches_label_key_root_cwd_equality_case_insensitive():
    t = _thread()
    assert matches(t, "repo")
    assert matches(t, "REPO")                       # case-insensitive
    assert matches(t, "github.com/me/repo")         # key
    assert matches(t, "/r/repo")                    # git root (exact)
    assert matches(t, "/r/repo/sub")                # cwd (exact)


def test_matches_path_prefix_on_root_and_cwd():
    t = _thread()
    assert matches(t, "/r")                          # prefix of root + cwd
    assert matches(t, "/r/repo/s")                   # prefix of cwd


def test_matches_non_match():
    t = _thread()
    assert not matches(t, "other-repo")
    assert not matches(t, "/different/path")


def test_matches_tolerates_missing_git_fields():
    t = {"sessionId": "x", "project": "/only/cwd",
         "gitProjectLabel": None, "gitProjectKey": None, "gitRoot": None}
    assert matches(t, "/only/cwd")
    assert matches(t, "/only")
    assert not matches(t, "repo")


def test_filter_by_projects_any_match_and_empty_is_all():
    a = _thread("a", git_label="repo-a", git_key="k-a", git_root="/r/a", project="/r/a")
    b = _thread("b", git_label="repo-b", git_key="k-b", git_root="/r/b", project="/r/b")
    assert filter_by_projects([a, b], None) == [a, b]      # None → all
    assert filter_by_projects([a, b], []) == [a, b]        # empty → all
    assert filter_by_projects([a, b], ["repo-a"]) == [a]
    assert filter_by_projects([a, b], ["repo-a", "repo-b"]) == [a, b]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_projects.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.projects'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/projects.py`:

```python
from __future__ import annotations


def matches(thread: dict, selector: str) -> bool:
    sel = selector.strip().lower()
    # equality against label / key / root / cwd
    for key in ("gitProjectLabel", "gitProjectKey", "gitRoot", "project"):
        val = thread.get(key)
        if val and val.lower() == sel:
            return True
    # path-prefix against root / cwd
    for key in ("gitRoot", "project"):
        val = thread.get(key)
        if val and val.lower().startswith(sel):
            return True
    return False


def filter_by_projects(threads, selectors) -> list[dict]:
    if not selectors:
        return list(threads)
    return [t for t in threads if any(matches(t, s) for s in selectors)]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_projects.py -q
```

Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/projects.py tests/test_projects.py
git commit -m "feat: project selector matching and filtering"
```

---

### Task 3: `projects.py` — `project_summary`

Per-project counts for the `st-projects` menu. Reuses `overview.group_key` / `group_label` for grouping consistency.

**Files:**
- Modify: `sessiontracker/projects.py`
- Test: `tests/test_projects.py` (append)

- [ ] **Step 1: Write the failing test (append to `tests/test_projects.py`)**

```python
from sessiontracker.projects import project_summary, ProjectStat


def _t(sid, status="finished", title=None, git_key="k", git_label="repo", project="/p"):
    return {
        "sessionId": sid, "status": status, "title": title,
        "project": project, "gitProjectKey": git_key, "gitProjectLabel": git_label,
        "gitRoot": None,
    }


def _reg(threads):
    return {"version": 1, "lastScan": None, "threads": threads, "tombstones": []}


def test_project_summary_counts_total_classified_unnamed():
    reg = _reg([
        _t("a", status="finished", title=None,    git_key="k1", git_label="repo1"),
        _t("b", status="blocked",  title="named",  git_key="k1", git_label="repo1"),
        _t("c", status="unclassified", title=None, git_key="k1", git_label="repo1"),
    ])
    stats = project_summary(reg)
    assert len(stats) == 1
    s = stats[0]
    assert isinstance(s, ProjectStat)
    assert s.label == "repo1"
    assert s.total == 3
    assert s.classified == 2          # finished + blocked
    assert s.unnamed == 1             # classified AND no title → only "a"


def test_project_summary_orders_git_first_then_by_total_desc():
    reg = _reg([
        _t("a", git_key="k-big", git_label="big"),
        _t("b", git_key="k-big", git_label="big"),
        _t("c", git_key="k-small", git_label="small"),
        # non-git cwd group (no gitProjectKey)
        _t("d", git_key=None, git_label=None, project="/lonely"),
    ])
    stats = project_summary(reg)
    labels = [s.label for s in stats]
    assert labels[0] == "big"          # git, total 2
    assert labels[1] == "small"        # git, total 1
    assert labels[-1] == "/lonely"     # non-git bucket last
    assert stats[0].is_git is True
    assert stats[-1].is_git is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_projects.py -q
```

Expected: FAIL — `ImportError: cannot import name 'project_summary'`.

- [ ] **Step 3: Add the implementation to `sessiontracker/projects.py`**

Add the import and the new code:

```python
from __future__ import annotations

from dataclasses import dataclass

from . import overview
```

(Replace the existing bare `from __future__ import annotations` line at the top with the block above.)

Then append:

```python
@dataclass
class ProjectStat:
    key: str
    label: str
    is_git: bool
    total: int
    classified: int
    unnamed: int


def project_summary(reg: dict) -> list[ProjectStat]:
    groups: dict[str, dict] = {}
    for t in reg["threads"]:
        key = overview.group_key(t)
        g = groups.get(key)
        if g is None:
            g = {"label": overview.group_label(t),
                 "is_git": bool(t.get("gitProjectKey")),
                 "total": 0, "classified": 0, "unnamed": 0}
            groups[key] = g
        g["total"] += 1
        if t.get("status", "unclassified") != "unclassified":
            g["classified"] += 1
            if not t.get("title"):
                g["unnamed"] += 1

    stats = [ProjectStat(key=k, label=g["label"], is_git=g["is_git"],
                         total=g["total"], classified=g["classified"],
                         unnamed=g["unnamed"])
             for k, g in groups.items()]
    # git groups first, then most sessions first, then label
    stats.sort(key=lambda s: (not s.is_git, -s.total, s.label.lower()))
    return stats
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_projects.py -q
```

Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/projects.py tests/test_projects.py
git commit -m "feat: project summary stats"
```

---

### Task 4: `naming.py` — `title_line` + `unique_name`

**Files:**
- Create: `sessiontracker/naming.py`
- Test: `tests/test_naming.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_naming.py`:

```python
import json
from sessiontracker.naming import title_line, unique_name


def test_title_line_exact_format():
    line = title_line("11111111-2222-3333-4444-555555555555", "login-button")
    obj = json.loads(line)
    assert obj == {
        "type": "custom-title",
        "customTitle": "login-button",
        "sessionId": "11111111-2222-3333-4444-555555555555",
    }
    assert "\n" not in line          # caller adds the newline


def test_unique_name_passes_through_when_free():
    assert unique_name("login-button", "abcdef12-0000-0000-0000-000000000000", set()) == "login-button"


def test_unique_name_suffixes_on_collision():
    taken = {"login-button"}
    out = unique_name("login-button", "abcdef12-3456-0000-0000-000000000000", taken)
    assert out == "login-button-abcdef"     # first 6 hex of sessionId, dashes stripped
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_naming.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.naming'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/naming.py`:

```python
from __future__ import annotations

import json

from . import overview

ACTIVE_WINDOW_MINUTES = 10


def title_line(session_id: str, name: str) -> str:
    return json.dumps(
        {"type": "custom-title", "customTitle": name, "sessionId": session_id},
        ensure_ascii=False,
    )


def _short_suffix(session_id: str) -> str:
    return session_id.replace("-", "")[:6]


def unique_name(suggested: str, session_id: str, taken) -> str:
    if suggested not in taken:
        return suggested
    return f"{suggested}-{_short_suffix(session_id)}"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_naming.py -q
```

Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/naming.py tests/test_naming.py
git commit -m "feat: title line and unique-name helpers"
```

---

### Task 5: `naming.py` — `candidate_kind`

The eligibility table: returns `"new"`, `"rename"`, or `None`.

**Files:**
- Modify: `sessiontracker/naming.py`
- Test: `tests/test_naming.py` (append)

- [ ] **Step 1: Write the failing test (append to `tests/test_naming.py`)**

```python
from sessiontracker.naming import candidate_kind

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-01T00:00:00+00:00"          # 29 days old → not active


def _thread(sid="s1", mtime=OLD, title=None, applied=None,
            suggested="thing-x", status="finished"):
    return {
        "sessionId": sid, "mtime": mtime, "title": title,
        "appliedName": applied, "suggestedName": suggested, "status": status,
    }


def test_kind_new_when_no_title():
    assert candidate_kind(_thread(title=None), NOW) == "new"


def test_kind_rename_when_our_name_drifted():
    t = _thread(title="old-name", applied="old-name", suggested="new-name")
    assert candidate_kind(t, NOW) == "rename"


def test_kind_none_when_user_owned_title():
    # title set, but not equal to appliedName → user named it
    assert candidate_kind(_thread(title="mine", applied=None, suggested="x")) is None
    assert candidate_kind(_thread(title="mine", applied="ours", suggested="x")) is None


def test_kind_none_when_converged():
    t = _thread(title="name", applied="name", suggested="name")
    assert candidate_kind(t, NOW) is None


def test_kind_none_when_unclassified_or_no_suggested():
    assert candidate_kind(_thread(status="unclassified"), NOW) is None
    assert candidate_kind(_thread(suggested=None), NOW) is None


def test_kind_none_when_active():
    # mtime == now → age 0 → within active window → skip
    assert candidate_kind(_thread(mtime=NOW, title=None), NOW) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_naming.py -q
```

Expected: FAIL — `ImportError: cannot import name 'candidate_kind'`.

- [ ] **Step 3: Add the implementation to `sessiontracker/naming.py`**

```python
def _age_minutes(thread: dict, now_iso: str) -> float:
    return overview.age_days(thread["mtime"], now_iso) * 1440.0


def candidate_kind(thread: dict, now_iso: str,
                   active_window_minutes: float = ACTIVE_WINDOW_MINUTES):
    suggested = thread.get("suggestedName")
    if not suggested:
        return None
    if thread.get("status", "unclassified") == "unclassified":
        return None
    if _age_minutes(thread, now_iso) < active_window_minutes:
        return None                                  # too recently active
    title = thread.get("title")
    if not title:
        return "new"
    if title == thread.get("appliedName") and suggested != title:
        return "rename"
    return None                                      # user-owned or converged
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_naming.py -q
```

Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/naming.py tests/test_naming.py
git commit -m "feat: name/re-name eligibility classification"
```

---

### Task 6: `naming.py` — `select_eligible`

Walks the registry, picks candidates (ordered by `mtime`), assigns collision-free names, and tags each `new`/`rename`.

**Files:**
- Modify: `sessiontracker/naming.py`
- Test: `tests/test_naming.py` (append)

- [ ] **Step 1: Write the failing test (append to `tests/test_naming.py`)**

```python
from sessiontracker.naming import select_eligible


def _reg(threads):
    return {"version": 1, "lastScan": None, "threads": threads, "tombstones": []}


def test_select_returns_new_and_rename_ordered_by_mtime():
    older = _thread("a", mtime="2026-06-01T00:00:00+00:00", title=None, suggested="alpha")
    newer = _thread("b", mtime="2026-06-10T00:00:00+00:00",
                    title="old-b", applied="old-b", suggested="beta")
    reg = _reg([newer, older])                       # registry order shuffled
    out = select_eligible(reg, NOW)
    assert [(t["sessionId"], name, kind) for t, name, kind in out] == [
        ("a", "alpha", "new"),                       # older first
        ("b", "beta", "rename"),
    ]


def test_select_suffixes_colliding_names():
    a = _thread("aaaaaaaa-1111-0000-0000-000000000000",
                mtime="2026-06-01T00:00:00+00:00", title=None, suggested="dup")
    b = _thread("bbbbbbbb-2222-0000-0000-000000000000",
                mtime="2026-06-02T00:00:00+00:00", title=None, suggested="dup")
    out = select_eligible(_reg([a, b]), NOW)
    names = [name for _, name, _ in out]
    assert names == ["dup", "dup-bbbbbb"]            # second collides → suffixed


def test_select_skips_user_owned_and_converged():
    user = _thread("u", title="mine", applied=None, suggested="x")
    conv = _thread("c", title="name", applied="name", suggested="name")
    out = select_eligible(_reg([user, conv]), NOW)
    assert out == []


def test_rename_reconverges_after_apply():
    # before: our name drifted → rename candidate
    t = _thread("s", title="old", applied="old", suggested="new")
    assert select_eligible(_reg([t]), NOW)[0][2] == "rename"
    # simulate apply_name: title + appliedName become the new name
    t["title"] = "new"
    t["appliedName"] = "new"
    assert select_eligible(_reg([t]), NOW) == []     # converged → nothing to do
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_naming.py -q
```

Expected: FAIL — `ImportError: cannot import name 'select_eligible'`.

- [ ] **Step 3: Add the implementation to `sessiontracker/naming.py`**

```python
def select_eligible(reg: dict, now_iso: str,
                    active_window_minutes: float = ACTIVE_WINDOW_MINUTES):
    taken = {t["title"] for t in reg["threads"] if t.get("title")}
    out = []
    for t in sorted(reg["threads"], key=lambda t: t["mtime"]):
        kind = candidate_kind(t, now_iso, active_window_minutes)
        if kind is None:
            continue
        final = unique_name(t["suggestedName"], t["sessionId"], taken)
        taken.add(final)
        out.append((t, final, kind))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_naming.py -q
```

Expected: PASS (13 passed).

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS (all files green).

- [ ] **Step 6: Commit**

```bash
git add sessiontracker/naming.py tests/test_naming.py
git commit -m "feat: select eligible sessions for naming"
```

---

### Task 7: `bin/st-name` CLI

Thin side-effecting wrapper: select → (dry-run print | append titles + update registry).

**Files:**
- Create: `bin/st-name`

- [ ] **Step 1: Write the implementation**

Create `bin/st-name`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry, naming, projects, overview  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_title(file_path: str, line: str) -> None:
    p = Path(file_path)
    data = p.read_bytes()
    lead = b"" if (not data or data.endswith(b"\n")) else b"\n"
    with p.open("ab") as fh:
        fh.write(lead + line.encode("utf-8") + b"\n")


def main():
    ap = argparse.ArgumentParser(prog="st-name")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--project", action="append", default=None,
                    help="repo/dir selector; repeatable")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--active-window", type=float, default=naming.ACTIVE_WINDOW_MINUTES)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = _now()
    reg = registry.load_registry(args.registry)
    selected = naming.select_eligible(reg, now, active_window_minutes=args.active_window)

    if args.project:
        selected = [s for s in selected
                    if any(projects.matches(s[0], sel) for sel in args.project)]
    if args.limit is not None:
        selected = selected[:args.limit]

    if args.dry_run:
        for t, name, kind in selected:
            label = overview.group_label(t)
            if kind == "new":
                print(f"new:    {name}  ({label}) — {t.get('whatItIs') or ''}")
            else:
                print(f"rename: {t['title']} -> {name}  ({label})")
        print(f"({len(selected)} to apply)")
        return

    new_n = ren_n = skipped = 0
    for t, name, kind in selected:
        try:
            _append_title(t["file"], naming.title_line(t["sessionId"], name))
        except OSError as e:
            print(f"skipped {t['sessionId']}: {e}", file=sys.stderr)
            skipped += 1
            continue
        registry.apply_name(reg, t["sessionId"], name)
        if kind == "new":
            new_n += 1
        else:
            ren_n += 1
    registry.save_registry(args.registry, reg)
    print(f"named {new_n}, renamed {ren_n}, skipped {skipped}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable + smoke-test dry-run and apply against a temp registry/transcript**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-name
TMP=$(mktemp -d)
# a real session transcript (one user line) + a registry that already classified it
printf '%s\n' '{"type":"user","cwd":"/Users/me/proj","message":{"role":"user","content":"add a login button"}}' > "$TMP/sess.jsonl"
.venv/bin/python - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
reg = {"version":1,"lastScan":"2026-06-30T00:00:00+00:00","tombstones":[],
 "threads":[{"sessionId":"sess","file":f"{tmp}/sess.jsonl","project":"/Users/me/proj",
   "title":None,"aiTitle":None,"summary":None,"lastPrompt":"lp",
   "mtime":"2026-06-01T00:00:00+00:00","sizeBytes":10,"messageCount":1,
   "gitBranch":None,"gitProjectKey":None,"gitProjectLabel":None,"gitRoot":None,"gitRemote":None,
   "status":"unfinished","statusConfidence":0.6,"whatItIs":"add a login button",
   "suggestedName":"login-button","appliedName":None,"namedAtMtime":None,
   "classifiedAtMtime":"2026-06-01T00:00:00+00:00","decision":None,"harvestPath":None,
   "todoId":None,"tags":[]}]}
json.dump(reg, open(f"{tmp}/registry.json","w"), indent=2)
PY
echo "=== dry-run ==="
.venv/bin/python bin/st-name --registry "$TMP/registry.json" --dry-run
echo "=== apply ==="
.venv/bin/python bin/st-name --registry "$TMP/registry.json"
echo "=== transcript now ends with our custom-title? ==="
tail -1 "$TMP/sess.jsonl"
echo "=== registry title/appliedName updated? ==="
.venv/bin/python -c "import json;t=json.load(open('$TMP/registry.json'))['threads'][0];print(t['title'],t['appliedName'])"
echo "=== second run is a no-op (converged)? ==="
.venv/bin/python bin/st-name --registry "$TMP/registry.json"
rm -rf "$TMP"
```

Expected: dry-run prints `new:    login-button ...` and `(1 to apply)`; apply prints `named 1, renamed 0, skipped 0`; the transcript's last line is `{"type": "custom-title", "customTitle": "login-button", "sessionId": "sess"}`; registry shows `login-button login-button`; the second run prints `named 0, renamed 0, skipped 0`.

- [ ] **Step 3: Commit**

```bash
git add bin/st-name
git commit -m "feat: st-name CLI"
```

---

### Task 8: `bin/st-projects` CLI

**Files:**
- Create: `bin/st-projects`

- [ ] **Step 1: Write the implementation**

Create `bin/st-projects`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry, projects  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def main():
    ap = argparse.ArgumentParser(prog="st-projects")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args()

    reg = registry.load_registry(args.registry)
    stats = projects.project_summary(reg)

    print(f"{'PROJECT':40} {'total':>5} {'class':>5} {'unnamed':>7}")
    for s in stats:
        marker = "" if s.is_git else "  (no git)"
        print(f"{(s.label or '(unknown)')[:40]:40} {s.total:5} {s.classified:5} {s.unnamed:7}{marker}")
    print(f"\n{len(stats)} projects/dirs with sessions")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable + smoke-test against a temp registry**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-projects
TMP=$(mktemp -d)
.venv/bin/python - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
def th(sid, key, label, status="finished", title=None, project="/p"):
    return {"sessionId":sid,"status":status,"title":title,"project":project,
            "gitProjectKey":key,"gitProjectLabel":label,"gitRoot":None}
reg = {"version":1,"lastScan":None,"tombstones":[],"threads":[
    th("a","k1","repo1",title=None), th("b","k1","repo1",title="named"),
    th("c","k2","repo2",status="unclassified"),
    th("d",None,None,project="/lonely")]}
json.dump(reg, open(f"{tmp}/registry.json","w"))
PY
.venv/bin/python bin/st-projects --registry "$TMP/registry.json"
rm -rf "$TMP"
```

Expected: a table with `repo1` (total 2, class 1, unnamed 1) first, then `repo2`, then `/lonely` flagged `(no git)`, and `3 projects/dirs with sessions`.

- [ ] **Step 3: Commit**

```bash
git add bin/st-projects
git commit -m "feat: st-projects CLI"
```

---

### Task 9: scope `st-sync scan` by `--project`

The scan still reconciles the whole registry; `--project` only filters the emitted pending list, so a scoped sync classifies just that repo/dir.

**Files:**
- Modify: `bin/st-sync`

- [ ] **Step 1: Update the imports and `cmd_scan`**

In `bin/st-sync`, change the import line:

```python
from sessiontracker import scanner, registry  # noqa: E402
```

to:

```python
from sessiontracker import scanner, registry, projects  # noqa: E402
```

Then in `cmd_scan`, change:

```python
    pending = registry.sessions_needing_classification(reg)
```

to:

```python
    pending = registry.sessions_needing_classification(reg)
    pending = projects.filter_by_projects(pending, getattr(args, "project", None))
```

- [ ] **Step 2: Add the `--project` flag to the `scan` subparser**

In `main()`, find the `scan` subparser block and add the flag:

```python
    s = sub.add_parser("scan", help="reconcile registry; emit pending classifications")
    s.add_argument("--tail", type=int, default=30)
    s.add_argument("--project", action="append", default=None,
                   help="repo/dir selector; repeatable — filters emitted pending")
    s.set_defaults(fn=cmd_scan)
```

- [ ] **Step 3: Smoke-test the filter against a temp projects dir**

```bash
cd ~/wrksp/incubation/session-tracker
TMP=$(mktemp -d)
mkdir -p "$TMP/-Users-me-a" "$TMP/-Users-me-b"
printf '%s\n' '{"type":"user","cwd":"/Users/me/a","message":{"role":"user","content":"work A"}}' > "$TMP/-Users-me-a/aaaaaaaa-0000-0000-0000-000000000001.jsonl"
printf '%s\n' '{"type":"user","cwd":"/Users/me/b","message":{"role":"user","content":"work B"}}' > "$TMP/-Users-me-b/bbbbbbbb-0000-0000-0000-000000000002.jsonl"
echo "=== unscoped: 2 pending ==="
SESSION_TRACKER_PROJECTS_DIR="$TMP" .venv/bin/python bin/st-sync --registry "$TMP/reg.json" scan \
  | .venv/bin/python -c "import sys,json;print('pending',json.load(sys.stdin)['pendingCount'])"
echo "=== scoped to /Users/me/a: 1 pending ==="
SESSION_TRACKER_PROJECTS_DIR="$TMP" .venv/bin/python bin/st-sync --registry "$TMP/reg.json" scan --project /Users/me/a \
  | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print('pending',d['pendingCount'],'proj',d['pending'][0]['project'])"
rm -rf "$TMP"
```

Expected: first prints `pending 2`; second prints `pending 1 proj /Users/me/a`.

- [ ] **Step 4: Confirm the suite still passes + commit**

```bash
.venv/bin/pytest -q
git add bin/st-sync
git commit -m "feat: scope st-sync scan by project"
```

Expected: tests green.

---

### Task 10: `session-name` skill

**Files:**
- Create: `.claude/skills/session-name/SKILL.md`

- [ ] **Step 1: Write the skill**

Create `.claude/skills/session-name/SKILL.md`:

```markdown
---
name: session-name
description: Use to give dormant Claude Code sessions good, accurate names so the picker is readable and `claude --resume <name>` works. Names newly-classified sessions and re-names ones whose focus drifted. Scope to a repo/dir to roll out gradually.
---

# session-name

Applies the registry's `suggestedName`s to the real sessions by appending a
`custom-title` record to each transcript. It does NOT decide what a session is
about — that's `session-sync`'s classification. This skill only applies/refreshes
names, and only ever touches names *it* generated (never a name you set by hand).

## Steps

Work from the repo root (`~/wrksp/incubation/session-tracker`). Scope everything
to one repo/dir at a time with `--project <selector>` (a git-project label, or a
cwd path; repeatable).

1. **See which repos/dirs have sessions** (to pick a scope):

   ```bash
   cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-projects
   ```

2. **Make sure that scope is classified/fresh** — run the `session-sync` skill
   scoped to it (`st-sync scan --project <selector>`), classifying any pending
   sessions. Re-naming depends on fresh classifications.

3. **Preview** what would be named/re-named in that scope:

   ```bash
   cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-name --project <selector> --dry-run
   ```

   `new:` rows are first-time names; `rename:` rows show `old -> new` where a
   session we previously named has drifted. Use `--limit N` to try a few first.

4. **Apply:**

   ```bash
   cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-name --project <selector>
   ```

5. **Report** the summary (named / renamed / skipped).

## First-use check
After the first apply, open the picker (or `claude --resume <name>`) for one
named session and confirm the new name shows. This verifies the appended record
is surfaced by CC (we couldn't test the interactive picker during the build).

## Notes
- Idempotent: re-running only touches sessions that are newly named or have
  drifted since we named them; converged ones are skipped.
- Never hand-edit `registry.json` or the `.jsonl` transcripts.
- Recently-active sessions (modified in the last ~10 min) are skipped to avoid
  racing CC writing the same file.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/session-name/SKILL.md
git commit -m "feat: session-name skill"
```

---

### Task 11: extend `session-overview` skill with a projects view

**Files:**
- Modify: `.claude/skills/session-overview/SKILL.md`

- [ ] **Step 1: Append a "list projects" section**

Open `.claude/skills/session-overview/SKILL.md` and add this section just before its `## Notes` section (if there's no `## Notes`, append at the end):

```markdown
## List the repos/dirs with sessions

To answer "which repos/dirs do I have sessions for?" (e.g. to pick a scope for
naming or syncing), run:

```bash
cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-projects
```

It prints each git project (and non-git cwd) with total / classified / unnamed
session counts, git projects first, most sessions first.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/session-overview/SKILL.md
git commit -m "feat: list-projects in session-overview skill"
```

---

### Task 12: End-to-end validation (real registry, mostly read-only)

Validate against the real `registry.json` with `--dry-run` (no writes), and do one real write against a **throwaway copy** of a transcript so nothing real is mutated.

**Files:** none (validation only)

- [ ] **Step 1: `st-projects` on the real registry**

```bash
cd ~/wrksp/incubation/session-tracker
.venv/bin/python bin/st-projects | head -25
```

Expected: a real per-repo table; counts roughly consistent with the ~233 sessions / ~18 git projects seen earlier; no traceback.

- [ ] **Step 2: `st-name --dry-run` on the real registry (writes nothing)**

```bash
.venv/bin/python bin/st-name --dry-run | head -30
```

Expected: `new:`/`rename:` rows for the currently-classified sessions (the ~15 classified so far → a handful of `new:` rows), ending with `(N to apply)`. No `.jsonl` is modified (dry-run). Confirm with: nothing should change — `git status` still clean for tracked files, and `registry.json` is gitignored anyway.

- [ ] **Step 3: Real write against a throwaway transcript copy**

```bash
cd ~/wrksp/incubation/session-tracker
TMP=$(mktemp -d)
# copy ONE real classified session's transcript into a temp dir, point a temp registry at the copy
.venv/bin/python - "$TMP" <<'PY'
import json, shutil, sys
tmp = sys.argv[1]
reg = json.load(open("registry.json"))
t = next(x for x in reg["threads"] if x["status"] != "unclassified" and not x["title"] and x["suggestedName"])
dst = f"{tmp}/copy.jsonl"
shutil.copyfile(t["file"], dst)
t = dict(t); t["file"] = dst
json.dump({"version":1,"lastScan":reg["lastScan"],"tombstones":[],"threads":[t]},
          open(f"{tmp}/registry.json","w"), indent=2)
print("using session", t["sessionId"], "->", t["suggestedName"])
PY
.venv/bin/python bin/st-name --registry "$TMP/registry.json"
echo "=== appended record on the COPY (real file untouched) ==="
tail -1 "$TMP/copy.jsonl"
rm -rf "$TMP"
```

Expected: prints `named 1, renamed 0, skipped 0`; the copy's last line is a `custom-title` record with the session's `suggestedName`. The real transcript under `~/.claude/projects` is never touched.

- [ ] **Step 4: Final full suite**

```bash
.venv/bin/pytest -q
```

Expected: all green. (No code to commit unless a step surfaced a fix — if so, fix + add a regression test before committing.)

---

## Self-review against the spec

- **Done lives in sync, not here:** no classification logic added; `st-name` consumes `suggestedName`. ✓
- **Provenance (`appliedName`/`namedAtMtime`):** Task 1 adds fields + `apply_name`; reconcile preserves them (Task 1 test). ✓
- **Eligibility table (new / rename / skip-user / skip-converged / skip-active / skip-unclassified):** Task 5 `candidate_kind` + tests cover every row. ✓
- **Churn bound / re-convergence:** Task 6 test `test_rename_reconverges_after_apply`. ✓
- **Append mechanism + trailing-newline + last-wins rename:** Task 7 `_append_title` + smoke test (rename = another append). ✓
- **Collision suffix (6 hex of sessionId):** Task 4 `unique_name` + Task 6 `select` suffix test. ✓
- **Scoping selector (label/key/root/cwd + path-prefix, case-insensitive, repeatable):** Task 2 `matches`/`filter_by_projects`; applied in `st-name` (Task 7) and `st-sync scan` (Task 9). ✓
- **`st-projects` view (total/classified/unnamed, git-first):** Tasks 3 + 8. ✓
- **Skills:** `session-name` (Task 10), `session-overview` projects view (Task 11). ✓
- **Auto-apply + `--dry-run` (distinguishes new vs rename) + `--limit`:** Task 7. ✓
- **No backups/undo:** none added. ✓
- **Active-window guard:** Task 5 `candidate_kind` + test. ✓
```
