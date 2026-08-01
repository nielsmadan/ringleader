# project-cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the "clean up a project" curation flow — render/export sessions, harvest their knowledge into the repo's docs via `/doc`, archive finished transcripts out of `~/.claude/projects`, and an interactive `clean-project` skill tying prune + triage together.

**Architecture:** Build order **B → A → C**. B = deterministic session-tracker CLIs + registry helpers (TDD'd Python). A = a `--session` mode added to the `/doc` skill at `~/ac/claude/skills/doc` (skill prose; verification, not unit tests). C = two new agentic skills in this repo (`session-harvest`, `clean-project`) that orchestrate B + A (skill prose; verification).

**Tech Stack:** Python 3.10+ (stdlib only at runtime), pytest (dev, `.venv`), Markdown SKILL.md files.

## Global Constraints

- Runtime code stdlib-only; pytest dev-only as `.venv/bin/pytest`.
- Work on `main` in session-tracker; commit per task. **Task 6 edits a different repo** (`~/ac/claude/skills/doc`) — commit there separately, and treat the `/doc` skill as broadly-used (additive change only, don't disturb existing modes).
- bin scripts: `REPO = Path(__file__).resolve().parent.parent`, `sys.path.insert(0, str(REPO))`, `from sessiontracker import ...` (match existing `bin/st-*`). Timestamps generated at the CLI edge.
- Registry tombstone shape: `{"sessionId","deletedAt","reason","harvestPath", ...}`; archived adds `"archivePath"`.
- Archive root: `<repo>/archive/<encoded-project-dir>/` where `<encoded-project-dir>` is `Path(thread["file"]).parent.name`. `archive/` is gitignored.
- `/doc --session` never touches `docs/product/`; integrates per existing doc conventions (`## Gotchas`, `## Why`, `## How it works (current state)`).

---

### Task 1: `export.render_conversation`

**Files:**
- Create: `sessiontracker/export.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `sessiontracker.transcript._text_blocks(message: dict) -> str` (existing).
- Produces: `render_conversation(path) -> str` — readable markdown of a session's conversation (USER/ASSISTANT turns; assistant tool calls noted; metadata records and malformed lines skipped).

- [ ] **Step 1: Write the failing test**

Create `tests/test_export.py`:

```python
import json
from pathlib import Path
from sessiontracker.export import render_conversation


def _write(p: Path, records):
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_render_includes_turns_and_tools_skips_metadata(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "custom-title", "customTitle": "x", "sessionId": "s"},   # metadata → skipped
        {"type": "user", "message": {"role": "user", "content": "fix the login bug"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Looking into it."},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]}},
        {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": "noise"}]}},   # no text → no turn
    ])
    out = render_conversation(f)
    assert "## USER" in out and "fix the login bug" in out
    assert "## ASSISTANT" in out and "Looking into it." in out
    assert "Bash" in out                       # tool noted
    assert "custom-title" not in out           # metadata not rendered
    assert "noise" not in out                  # tool_result not rendered as a turn


def test_render_tolerates_garbage_and_empty(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("not json\n\n{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"hi\"}}\n",
                 encoding="utf-8")
    out = render_conversation(f)
    assert "hi" in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_export.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.export'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/export.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from .transcript import _text_blocks


def render_conversation(path) -> str:
    out: list[str] = []
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "user":
                txt = _text_blocks(o.get("message", {})).strip()
                if txt:
                    out.append(f"## USER\n\n{txt}\n")
            elif t == "assistant":
                msg = o.get("message", {})
                txt = _text_blocks(msg).strip()
                if txt:
                    out.append(f"## ASSISTANT\n\n{txt}\n")
                content = msg.get("content")
                tools = [b.get("name") for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_use"] \
                    if isinstance(content, list) else []
                tools = [t for t in tools if t]
                if tools:
                    out.append(f"_(tools: {', '.join(tools)})_\n")
    return "\n".join(out)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_export.py -q
```

Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/export.py tests/test_export.py
git commit -m "feat: render a session transcript to readable conversation markdown"
```

---

### Task 2: registry helpers — harvest / review / archive + `reviewedAtMtime`

**Files:**
- Modify: `sessiontracker/registry.py`
- Test: `tests/test_registry.py` (append)

**Interfaces:**
- Consumes: existing `reconcile`, `prune_session`.
- Produces: `mark_harvested(reg, session_id, harvest_paths) -> dict` (sets `decision="harvested"`, `harvestPath=harvest_paths or None`); `mark_reviewed(reg, session_id) -> dict` (sets `reviewedAtMtime = that thread's mtime`); `archive_session(reg, session_id, archive_path, archived_at) -> dict` (pops thread, appends tombstone `{"sessionId","deletedAt":archived_at,"reason":"archived","archivePath":archive_path,"harvestPath":<carried over>}`; idempotent). New threads carry `reviewedAtMtime: None`, preserved across `reconcile`.

- [ ] **Step 1: Write the failing test (append to `tests/test_registry.py`)**

```python
from sessiontracker.registry import mark_harvested, mark_reviewed, archive_session


def test_new_thread_has_null_reviewed_marker():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    assert reg["threads"][0]["reviewedAtMtime"] is None


def test_reconcile_preserves_reviewed_marker():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    reg["threads"][0]["reviewedAtMtime"] = "2026-06-01T00:00:00+00:00"
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    assert reg2["threads"][0]["reviewedAtMtime"] == "2026-06-01T00:00:00+00:00"


def test_mark_harvested_sets_decision_and_paths():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    mark_harvested(reg, "s1", ["docs/tech/auth.md"])
    t = reg["threads"][0]
    assert t["decision"] == "harvested"
    assert t["harvestPath"] == ["docs/tech/auth.md"]
    mark_harvested(reg, "s1", [])              # nothing found → None
    assert reg["threads"][0]["harvestPath"] is None


def test_mark_reviewed_records_current_mtime():
    reg = reconcile(empty_registry(), [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    mark_reviewed(reg, "s1")
    assert reg["threads"][0]["reviewedAtMtime"] == "2026-06-05T00:00:00+00:00"


def test_archive_session_removes_thread_and_tombstones():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    reg["threads"][0]["harvestPath"] = ["docs/x.md"]
    archive_session(reg, "s1", "archive/enc/s1.jsonl", "2026-06-23T00:00:00+00:00")
    assert reg["threads"] == []
    ts = reg["tombstones"][0]
    assert ts["sessionId"] == "s1" and ts["reason"] == "archived"
    assert ts["archivePath"] == "archive/enc/s1.jsonl"
    assert ts["harvestPath"] == ["docs/x.md"]
    assert ts["deletedAt"] == "2026-06-23T00:00:00+00:00"


def test_mark_helpers_raise_for_unknown_id():
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    for fn in (lambda: mark_harvested(reg, "nope", []),
               lambda: mark_reviewed(reg, "nope")):
        with pytest.raises(KeyError):
            fn()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: FAIL — `ImportError: cannot import name 'mark_harvested'`.

- [ ] **Step 3: Implement**

In `sessiontracker/registry.py`, add `"reviewedAtMtime": None,` to the dict returned by `_new_thread` (next to the other null fields, e.g. right after `"namedAtMtime": None,`). Then append:

```python
def mark_harvested(reg: dict, session_id: str, harvest_paths) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["decision"] = "harvested"
            t["harvestPath"] = harvest_paths or None
            return reg
    raise KeyError(session_id)


def mark_reviewed(reg: dict, session_id: str) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["reviewedAtMtime"] = t["mtime"]
            return reg
    raise KeyError(session_id)


def archive_session(reg: dict, session_id: str, archive_path: str,
                    archived_at: str) -> dict:
    threads = reg["threads"]
    idx = next((i for i, t in enumerate(threads)
                if t["sessionId"] == session_id), None)
    harvest = threads[idx].get("harvestPath") if idx is not None else None
    if idx is not None:
        threads.pop(idx)
    tombstones = reg.setdefault("tombstones", [])
    if not any(ts["sessionId"] == session_id for ts in tombstones):
        tombstones.append({"sessionId": session_id, "deletedAt": archived_at,
                           "reason": "archived", "archivePath": archive_path,
                           "harvestPath": harvest})
    return reg
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: PASS (all registry tests green, incl. the 6 new ones).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/registry.py tests/test_registry.py
git commit -m "feat: registry harvest/review/archive helpers + reviewedAtMtime"
```

---

### Task 3: `bin/st-export`

**Files:**
- Create: `bin/st-export`
- Modify: `st` (usage line)
- Test: `tests/test_st_export.py`

**Interfaces:**
- Consumes: `registry.load_registry`, `export.render_conversation`.
- Produces: `st export <id> [--out <path>] [--registry <path>]` — render the session whose `sessionId == <id>` (path from the registry) to `--out` or stdout; exit 1 with a stderr message if the id is unknown.

- [ ] **Step 1: Write the implementation**

Create `bin/st-export`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry, export  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def main():
    ap = argparse.ArgumentParser(prog="st-export")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"st-export: unknown session {args.id}", file=sys.stderr)
        sys.exit(1)

    text = export.render_conversation(t["file"])
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable + add dispatcher usage line**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-export
```

In `st`, add inside the `usage()` heredoc after the `st prune` line:

```
  st export   <id> [--out <path>]   render a session to readable markdown
```

- [ ] **Step 3: Write the subprocess test**

Create `tests/test_st_export.py`:

```python
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"


def _setup(tmp, sid="a"):
    f = tmp / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "user", "message":
                 {"role": "user", "content": "hello world"}}) + "\n", encoding="utf-8")
    reg = tmp / "registry.json"
    reg.write_text(json.dumps({"version": 1, "lastScan": None, "tombstones": [],
                   "threads": [{"sessionId": sid, "file": str(f)}]}), encoding="utf-8")
    return reg


def _run(reg, *args):
    return subprocess.run([str(PY), str(REPO / "bin" / "st-export"),
                           "--registry", str(reg), *args],
                          cwd=str(REPO), capture_output=True, text=True)


def test_export_to_stdout(tmp_path):
    reg = _setup(tmp_path)
    r = _run(reg, "a")
    assert r.returncode == 0 and "hello world" in r.stdout


def test_export_to_file(tmp_path):
    reg = _setup(tmp_path)
    out = tmp_path / "conv.md"
    r = _run(reg, "a", "--out", str(out))
    assert r.returncode == 0
    assert "hello world" in out.read_text()


def test_unknown_id_exits_1(tmp_path):
    reg = _setup(tmp_path)
    r = _run(reg, "nope")
    assert r.returncode == 1 and "unknown session" in r.stderr
```

- [ ] **Step 4: Run the test**

```bash
.venv/bin/pytest tests/test_st_export.py -q
```

Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add bin/st-export st tests/test_st_export.py
git commit -m "feat: st export CLI"
```

---

### Task 4: `bin/st-archive` + `bin/st-delete`

**Files:**
- Create: `bin/st-archive`, `bin/st-delete`
- Modify: `st` (usage lines), `.gitignore` (add `archive/`)
- Test: `tests/test_st_curation.py`

**Interfaces:**
- Consumes: `registry.{load_registry,save_registry,archive_session,prune_session}`.
- Produces: `st archive <id>` (move `<uuid>.jsonl`+`<uuid>/` sidecar to `archive/<encoded>/`, tombstone `archived`); `st delete <id>` (delete files, tombstone `reason="deleted"`).

- [ ] **Step 1: Add `archive/` to `.gitignore`**

Append to `.gitignore`:

```gitignore
# archived raw transcripts (machine-local retention)
archive/
```

- [ ] **Step 2: Write `bin/st-archive`**

Create `bin/st-archive`:

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

from sessiontracker import registry  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(prog="st-archive")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--archive-root", default=str(REPO / "archive"))
    args = ap.parse_args()

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"st-archive: unknown session {args.id}", file=sys.stderr)
        sys.exit(1)

    src = Path(t["file"])
    dest_dir = Path(args.archive_root) / src.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        print(f"st-archive: {dest} already exists, aborting", file=sys.stderr)
        sys.exit(1)

    if src.exists():
        shutil.move(str(src), str(dest))
    sidecar = src.with_suffix("")
    if sidecar.exists():
        shutil.move(str(sidecar), str(dest_dir / sidecar.name))

    registry.archive_session(reg, args.id, str(dest), _now())
    registry.save_registry(args.registry, reg)
    print(f"archived {args.id} -> {dest}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write `bin/st-delete`**

Create `bin/st-delete`:

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

from sessiontracker import registry  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(prog="st-delete")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args()

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"st-delete: unknown session {args.id}", file=sys.stderr)
        sys.exit(1)

    f = Path(t["file"])
    f.unlink(missing_ok=True)
    shutil.rmtree(f.with_suffix(""), ignore_errors=True)
    registry.prune_session(reg, args.id, _now(), reason="deleted")
    registry.save_registry(args.registry, reg)
    print(f"deleted {args.id}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Make executable + dispatcher usage lines**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-archive bin/st-delete
```

In `st`, add inside `usage()` after the `st export` line:

```
  st archive  <id>                 move a finished session out to archive/ (not resumable)
  st delete   <id>                 delete one session (files + tombstone)
```

- [ ] **Step 5: Write the subprocess test**

Create `tests/test_st_curation.py`:

```python
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"


def _session(tmp, sid="a"):
    # mimic ~/.claude/projects/<encoded>/<uuid>.jsonl + sidecar
    proj = tmp / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{sid}.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    side = proj / sid
    side.mkdir()
    (side / "n.txt").write_text("x", encoding="utf-8")
    return f


def _registry(tmp, sid, f, *, harvest=None):
    reg = tmp / "registry.json"
    reg.write_text(json.dumps({"version": 1, "lastScan": None, "tombstones": [],
        "threads": [{"sessionId": sid, "file": str(f), "harvestPath": harvest}]}),
        encoding="utf-8")
    return reg


def _run(script, reg, *args):
    return subprocess.run([str(PY), str(REPO / "bin" / script),
                           "--registry", str(reg), *args],
                          cwd=str(REPO), capture_output=True, text=True)


def test_archive_moves_files_and_tombstones(tmp_path):
    f = _session(tmp_path, "a")
    reg = _registry(tmp_path, "a", f, harvest=["docs/x.md"])
    arc = tmp_path / "archive"
    r = _run("st-archive", reg, "a", "--archive-root", str(arc))
    assert r.returncode == 0 and "archived a" in r.stdout
    assert not f.exists() and not (f.parent / "a").exists()      # moved out
    archived = arc / "-Users-me-proj" / "a.jsonl"
    assert archived.exists()
    assert (arc / "-Users-me-proj" / "a").exists()              # sidecar moved too
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    ts = data["tombstones"][0]
    assert ts["reason"] == "archived" and ts["harvestPath"] == ["docs/x.md"]
    assert ts["archivePath"] == str(archived)


def test_delete_removes_files_and_tombstones(tmp_path):
    f = _session(tmp_path, "b")
    reg = _registry(tmp_path, "b", f)
    r = _run("st-delete", reg, "b")
    assert r.returncode == 0 and "deleted b" in r.stdout
    assert not f.exists() and not (f.parent / "b").exists()
    data = json.loads(reg.read_text())
    assert data["threads"] == []
    assert data["tombstones"][0]["reason"] == "deleted"


def test_archive_unknown_id_exits_1(tmp_path):
    reg = _registry(tmp_path, "a", tmp_path / "x.jsonl")
    assert _run("st-archive", reg, "nope").returncode == 1
```

- [ ] **Step 6: Run the test**

```bash
.venv/bin/pytest tests/test_st_curation.py -q
```

Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add bin/st-archive bin/st-delete st .gitignore tests/test_st_curation.py
git commit -m "feat: st archive + st delete CLIs"
```

---

### Task 5: `bin/st-mark-harvested` + `bin/st-review`

**Files:**
- Create: `bin/st-mark-harvested`, `bin/st-review`
- Modify: `st` (usage lines)
- Test: `tests/test_st_curation.py` (append)

**Interfaces:**
- Consumes: `registry.{load_registry,save_registry,mark_harvested,mark_reviewed}`.
- Produces: `st mark-harvested <id> [--paths a,b]` (set `decision="harvested"`, `harvestPath` = split list or None); `st review <id>` (set `reviewedAtMtime` to the thread's mtime).

- [ ] **Step 1: Write `bin/st-mark-harvested`**

Create `bin/st-mark-harvested`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def main():
    ap = argparse.ArgumentParser(prog="st-mark-harvested")
    ap.add_argument("id")
    ap.add_argument("--paths", default="", help="comma-separated doc paths harvested into")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args()

    paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    reg = registry.load_registry(args.registry)
    try:
        registry.mark_harvested(reg, args.id, paths)
    except KeyError:
        print(f"st-mark-harvested: unknown session {args.id}", file=sys.stderr)
        sys.exit(1)
    registry.save_registry(args.registry, reg)
    print(f"marked harvested {args.id} ({len(paths)} docs)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `bin/st-review`**

Create `bin/st-review`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def main():
    ap = argparse.ArgumentParser(prog="st-review")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args()

    reg = registry.load_registry(args.registry)
    try:
        registry.mark_reviewed(reg, args.id)
    except KeyError:
        print(f"st-review: unknown session {args.id}", file=sys.stderr)
        sys.exit(1)
    registry.save_registry(args.registry, reg)
    print(f"marked reviewed (open) {args.id}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Make executable + dispatcher usage lines**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-mark-harvested bin/st-review
```

In `st`, add inside `usage()` after the `st delete` line:

```
  st mark-harvested <id> [--paths a,b]   mark a session harvested (records docs)
  st review   <id>                 mark an open session reviewed (skip until it changes)
```

- [ ] **Step 4: Append the subprocess test (to `tests/test_st_curation.py`)**

```python
def test_mark_harvested_sets_decision(tmp_path):
    reg = _registry(tmp_path, "a", tmp_path / "a.jsonl")
    r = _run("st-mark-harvested", reg, "a", "--paths", "docs/x.md,docs/y.md")
    assert r.returncode == 0
    t = json.loads(reg.read_text())["threads"][0]
    assert t["decision"] == "harvested" and t["harvestPath"] == ["docs/x.md", "docs/y.md"]


def test_mark_harvested_empty_paths_is_none(tmp_path):
    reg = _registry(tmp_path, "a", tmp_path / "a.jsonl")
    _run("st-mark-harvested", reg, "a")
    assert json.loads(reg.read_text())["threads"][0]["harvestPath"] is None


def test_review_sets_reviewed_at_mtime(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 1, "lastScan": None, "tombstones": [],
        "threads": [{"sessionId": "a", "file": "x", "mtime": "2026-06-05T00:00:00+00:00",
                     "reviewedAtMtime": None}]}), encoding="utf-8")
    r = _run("st-review", reg, "a")
    assert r.returncode == 0
    assert json.loads(reg.read_text())["threads"][0]["reviewedAtMtime"] == "2026-06-05T00:00:00+00:00"
```

- [ ] **Step 5: Run the test + full suite**

```bash
.venv/bin/pytest tests/test_st_curation.py -q
.venv/bin/pytest -q
```

Expected: both green.

- [ ] **Step 6: Commit**

```bash
git add bin/st-mark-harvested bin/st-review st tests/test_st_curation.py
git commit -m "feat: st mark-harvested + st review CLIs"
```

---

### Task 6: `/doc --session` mode (in the `~/ac/claude/skills/doc` skill)

**This task edits a different repo** (`~/ac/claude/skills/doc`), and the `/doc` skill is broadly used — make an **additive** change only and commit in that repo.

**Files:**
- Modify: `~/ac/claude/skills/doc/SKILL.md`

**Interfaces:**
- Produces: a `--session` mode invoked as `/doc --session --md <conversation.md> [--report <file>]`. No code; the contract other tasks rely on is: it integrates the conversation's knowledge into the repo's docs and writes the newline-separated list of touched doc paths to `--report`.

- [ ] **Step 1: Read the existing skill to match its structure**

```bash
cat ~/ac/claude/skills/doc/SKILL.md
```

Note its frontmatter `description`, how modes/flags are listed (`--review`/`--update`/`--generate`), and the section style — the new content must match.

- [ ] **Step 2: Add the `--session` mode**

Update the frontmatter `description` to mention `--session` alongside the existing modes (one phrase, e.g. `…, or harvest a session log into the docs (--session)`). Then add this section (adapt headings to the file's style):

````markdown
## `--session` — harvest a session log into the docs

**Invocation:** `doc --session --md <conversation.md> [--report <file>]`

Integrate the durable knowledge from one work session into the project's docs. The
input `--md` file is a rendered conversation (plain markdown of USER/ASSISTANT turns)
— you do NOT read Claude Code `.jsonl` files here.

Extract and place three kinds of knowledge, using the SAME layout and conventions as
`--update`/`--generate`:

1. **Gotchas** — pitfalls, surprises, "things we ran into" → the `## Gotchas` section of
   the most relevant module/feature doc.
2. **Decision rationale** — why a product/tech decision was made → the `## Why` section.
3. **Behavior changes** — what the session actually changed → refresh the affected
   `## How it works (current state)` sections.

Rules:
- Find the right target docs via `docs/overview.md` and the existing tree. Prefer
  augmenting existing docs over creating new ones.
- **Never touch `docs/product/`** (owned by `review-product`).
- Only record durable, project-specific knowledge — skip transient chatter and
  anything already documented. It is fine to conclude there is nothing worth adding.
- **Preview** the proposed edits to the user and apply on confirmation. Leave changes
  **uncommitted** (the user commits).
- **Fallback:** if the repo has no `docs/`, write a single `docs/session-harvests/<name>.md`
  (derive `<name>` from the md filename) capturing the same three categories.
- **Report:** if `--report <file>` is given, write the list of doc files you created or
  edited to it, one path per line (repo-relative). If nothing was integrated, write an
  empty file. This is how the caller records what was harvested.
````

- [ ] **Step 3: Verify against a fixture**

```bash
mkdir -p /tmp/doctest/docs/tech
cat > /tmp/doctest/docs/overview.md <<'EOF'
# Docs
- tech/auth.md — auth module
EOF
cat > /tmp/doctest/docs/tech/auth.md <<'EOF'
# Auth
## How it works (current state)
Uses JWT.
## Gotchas
## Why
EOF
cat > /tmp/conv.md <<'EOF'
## USER
The token refresh kept failing — turns out the clock skew on CI broke JWT validation.
## ASSISTANT
Fixed by allowing 60s leeway. We chose JWT over sessions to stay stateless across workers.
EOF
```

Then, in a Claude Code session whose cwd is `/tmp/doctest`, run the skill: `/doc --session --md /tmp/conv.md --report /tmp/report.txt`. Confirm: the clock-skew gotcha landed in `docs/tech/auth.md`'s `## Gotchas`, the "JWT over sessions / stateless" rationale landed in `## Why`, `docs/product/` was not created/touched, and `/tmp/report.txt` lists `docs/tech/auth.md`. Report this verification in your task report (this is skill prose — there is no pytest).

- [ ] **Step 4: Commit (in the doc skill repo)**

```bash
cd ~/ac/claude/skills/doc
git add SKILL.md
git commit -m "feat: doc --session mode to harvest a session log into docs"
```

(If `~/ac` has commit hooks or a different flow, follow that repo's convention; do not push.)

---

### Task 7: `session-harvest` skill

**Files:**
- Create: `.claude/skills/session-harvest/SKILL.md`

**Interfaces:**
- Consumes: `st export`, `/doc --session`, `st mark-harvested`.
- Produces: the `session-harvest` skill (harvest one session into docs; does NOT archive).

- [ ] **Step 1: Write the skill**

Create `.claude/skills/session-harvest/SKILL.md`:

```markdown
---
name: session-harvest
description: Harvest one dormant Claude Code session's knowledge into its repo's docs — extract gotchas, decision rationale, and what-changed, and integrate them via the /doc skill. Use when a finished/old session has useful info worth keeping before you stop tracking it. Does not delete or archive the session.
---

# session-harvest

Capture the durable knowledge from a single session into the product's docs. The
deterministic bits are session-tracker CLIs; the doc integration is the `/doc` skill.

## Steps

Run from the session-tracker repo (`~/wrksp/incubation/session-tracker`). You need the
session's id (from `./st overview --all` or `./st project <repo>`).

1. **Export the conversation** to a temp markdown file:

   ```bash
   cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-export <id> --out /tmp/st-harvest-<id>.md
   ```

2. **Integrate into the repo's docs** by invoking the `/doc` skill with the export, from
   the *target repo's* working directory (the session's project). Pass a report file:

   `/doc --session --md /tmp/st-harvest-<id>.md --report /tmp/st-harvest-<id>.report`

   `/doc` previews the doc edits; confirm them with the user. It writes the list of
   touched doc paths to the report file.

3. **Record the harvest** in the registry (paths come from the report file, comma-joined):

   ```bash
   cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-mark-harvested <id> --paths "<comma-joined paths from the report>"
   ```

   If the report is empty (nothing worth documenting), run `st mark-harvested <id>` with
   no `--paths` — it still marks the session harvested.

4. **Report** which docs were updated and that the session is marked harvested.

## Notes
- This harvests only — it does NOT archive or delete. To move a finished session out of
  the resume picker afterward, use `./st archive <id>`.
- Never hand-edit `registry.json` or transcripts.
```

- [ ] **Step 2: Verify the commands resolve**

```bash
cd ~/wrksp/incubation/session-tracker
.venv/bin/python bin/st-export --help >/dev/null && echo export-ok
.venv/bin/python bin/st-mark-harvested --help >/dev/null && echo mark-ok
```

Expected: `export-ok` and `mark-ok`. (Full flow exercises `/doc`, which is agentic — verified in Task 6 and Task 8.)

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/session-harvest/SKILL.md
git commit -m "feat: session-harvest skill"
```

---

### Task 8: `clean-project` skill

**Files:**
- Create: `.claude/skills/clean-project/SKILL.md`

**Interfaces:**
- Consumes: `st prune`, `st project`, `st export`, `/doc --session`, `st mark-harvested`, `st archive`, `st delete`, `st review` (the `session-harvest` steps inline for "finished").
- Produces: the `clean-project` skill (the full guided cleanup flow).

- [ ] **Step 1: Write the skill**

Create `.claude/skills/clean-project/SKILL.md`:

```markdown
---
name: clean-project
description: Guided cleanup of one repo's dormant Claude Code sessions — first prune the tiny+stale junk, then go session-by-session to delete, finish (harvest knowledge into the docs + archive), or leave open. Use to triage a repo you have many old sessions for. Triggers "clean up my sessions for X", "triage project sessions".
---

# clean-project

Walk one repo's dormant sessions and resolve each. Deterministic actions are
session-tracker CLIs; harvesting into docs is the `/doc` skill.

Run from the session-tracker repo (`~/wrksp/incubation/session-tracker`). Ask the user
which repo/dir to clean (a git-project label or path) if not given — call it `<X>`.

## Steps

1. **Prune the junk first.** Preview, then (with the user's OK) delete:

   ```bash
   cd ~/wrksp/incubation/session-tracker && .venv/bin/python bin/st-prune --project <X>
   ```
   Show the preview; if the user approves, re-run with `--delete` (it asks its own y/N).

2. **Build the triage list.** Read the registry and select sessions in `<X>` that are
   still in `threads`, EXCLUDING any with `decision == "harvested"` and any where
   `reviewedAtMtime == mtime` (reviewed and untouched since). A quick way to see the
   repo's sessions and ids is `./st project <X> --all`. Order oldest-first.

3. **For each session, present a summary** — read it via
   `.venv/bin/python bin/st-export <id> --out /tmp/clean-<id>.md` (or the registry's
   `whatItIs`/`lastPrompt` for a fast pass) and tell the user, in 2-3 lines: what it was
   about, its status, age, size. Then ask: **delete / finished / open?**

   - **delete** →
     ```bash
     .venv/bin/python bin/st-delete <id>
     ```
   - **finished** → harvest, then archive:
     1. `/doc --session --md /tmp/clean-<id>.md --report /tmp/clean-<id>.report` (from the
        target repo's cwd; confirm the doc edits with the user).
     2. `.venv/bin/python bin/st-mark-harvested <id> --paths "<paths from report>"`
     3. `.venv/bin/python bin/st-archive <id>`  (moves it out of the resume picker)
   - **open** →
     ```bash
     .venv/bin/python bin/st-review <id>
     ```

4. **Tally** at the end: pruned N, deleted N, finished/harvested N, left open N.

## Notes
- "open" is not permanent — the session stays resumable and reappears next run only if it
  gets new activity. It can be finished or deleted in a later run.
- "finished" both harvests (knowledge → docs) and archives (raw transcript → `archive/`,
  out of `~/.claude/projects`). If `/doc` finds nothing worth keeping, still archive.
- Never hand-edit `registry.json` or transcripts.
```

- [ ] **Step 2: Verify the referenced commands exist**

```bash
cd ~/wrksp/incubation/session-tracker
for c in st-prune st-project st-export st-delete st-mark-harvested st-archive st-review; do
  .venv/bin/python bin/$c --help >/dev/null 2>&1 && echo "$c ok" || echo "$c MISSING"
done
```

Expected: every line `... ok`.

- [ ] **Step 3: Dry verification of the flow logic**

In a Claude Code session, walk the skill against the real registry but **only through step 2** (build the triage list and present the first session's summary) — do NOT delete/archive anything. Confirm the triage list correctly excludes harvested and reviewed-unchanged sessions. Report what you observed. (Full destructive flow is the user's to run.)

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/clean-project/SKILL.md
git commit -m "feat: clean-project skill"
```

---

## Self-review against the spec

- **A `/doc --session --md [--report]`, integrates per conventions, never `docs/product/`, fallback note, reports paths:** Task 6 (the section content covers all of it). ✓
- **B `st export` (render):** Tasks 1 (render_conversation) + 3 (CLI). ✓
- **B `st archive` (move + tombstone archived + archivePath/harvestPath):** Tasks 2 (archive_session) + 4 (CLI, `archive/<encoded>/`, .gitignore). ✓
- **B `st delete` (files + tombstone deleted):** Task 4 (reuses `prune_session(reason="deleted")`). ✓
- **B `st mark-harvested` / `st review` + `reviewedAtMtime` + reconcile-preserve:** Tasks 2 (helpers + field + preserve test) + 5 (CLIs). ✓
- **C `session-harvest` (export→/doc→mark, no archive):** Task 7. ✓
- **C `clean-project` (prune → triage excl. harvested + reviewed-unchanged → delete/finished/open):** Task 8. ✓
- **Data model (`reviewedAtMtime`, decision=harvested, tombstone reasons archived/deleted):** Tasks 2, 4. ✓
- **Seam contracts (C→B CLIs, C/harvest→/doc via report file, A is a leaf):** Tasks 6–8 wiring. ✓
- **Edge cases (unknown id exit 1, missing files tolerated, archive collision aborts, empty harvest still marks/archives):** Tasks 3–5 (exit 1, missing_ok), 4 (collision), 7/8 (empty harvest). ✓
- **Non-goals (no PATH, no auto-commit of docs/archive, gitignore archive):** Task 4 gitignore; nothing auto-commits user docs. ✓
