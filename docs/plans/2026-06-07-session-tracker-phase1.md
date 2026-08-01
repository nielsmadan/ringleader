# session-tracker Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase-1 core of session-tracker — `session-sync` (scan `~/.claude/projects`, reconcile a hybrid `registry.json`, classify changed sessions) and `session-overview` (render dormant threads by status/project with spread / WIP-debt metrics).

**Architecture:** A small stdlib-only Python package (`sessiontracker/`) holds all deterministic logic (transcript parsing, scanning, registry reconcile, overview rendering) and is fully unit-tested with TDD. Two thin CLI entrypoints in `bin/` wrap it. Two CC skills (`skills/session-sync`, `skills/session-overview`) orchestrate the LLM-in-the-loop classification: the sync CLI emits the sessions needing classification (with their conversation tails) as JSON, the skill agent classifies each, and writes results back via `bin/st-sync apply`. No third-party runtime deps; the package runs under the system `python3`. `pytest` is a dev-only dependency in a project venv.

**Tech Stack:** Python 3.10+ (stdlib only at runtime: `json`, `pathlib`, `dataclasses`, `datetime`, `argparse`), pytest (dev), Markdown SKILL.md files.

---

## Design decisions locked from SPEC.md

- **Project path** is read from the `cwd` field inside the transcript (present on nearly every user/assistant record), NOT decoded from the lossy encoded dir name.
- **Titles:** `custom-title.customTitle` (user-set → "named") and `ai-title.aiTitle` (CC auto). A session is "unnamed" iff it has no `custom-title`.
- **`/clear` boundaries are out of scope for Phase 1** (no clean marker; the tail already captures latest state). No `clearBoundaries` field yet.
- **Grouping by git project, not just `cwd`.** Many `cwd`s belong to one repo (worktrees, clones, monorepo subdirs). We resolve each distinct `cwd` to a git identity (`gitinfo.py`): normalized `remote.origin.url` slug (e.g. `github.com/org/repo`) as the group key, falling back to the repo toplevel, then to `cwd`. `gitBranch` is captured for free from transcript records. Git resolution shells out (best-effort, deduped per `cwd`, injectable for tests); results are cached in the registry and **preserved if the `cwd` later vanishes** (so a deleted checkout doesn't blank the grouping).
- **`abandoned` is a *derived* display status**, computed in code as `status == "unfinished" AND age > 21 days`, plus `decision == "abandon"`. The classifier only ever returns one of `active | unfinished | blocked | finished`. This keeps the LLM out of time math and avoids re-classifying just because time passed.
- **Hybrid (Approach C):** `reconcile()` refreshes metadata for every scanned file but only marks a thread as needing (re)classification when it's new or its `mtime` changed since `classifiedAtMtime`. User `decision`/`harvestPath`/`todoId`/`tags` are never overwritten.
- **Timestamps at the edge:** CLI entrypoints generate `now` (`datetime.now(timezone.utc).isoformat()`) and pass it into pure functions, so the logic stays deterministic/testable.

## File structure

```
session-tracker/
├── SPEC.md                                  (exists)
├── .gitignore                               Task 0
├── pytest.ini                               Task 0
├── sessiontracker/
│   ├── __init__.py                          Task 0
│   ├── transcript.py                        Task 1  parse one .jsonl → TranscriptInfo
│   ├── gitinfo.py                           Task 1b resolve cwd → git project identity
│   ├── scanner.py                           Task 2  enumerate files → list[SessionMeta]
│   ├── registry.py                          Task 3,4 load/save/reconcile/classify
│   └── overview.py                          Task 5  metrics + render + staleness
├── tests/
│   ├── test_transcript.py                   Task 1
│   ├── test_gitinfo.py                      Task 1b
│   ├── test_scanner.py                      Task 2
│   ├── test_registry.py                     Task 3,4
│   └── test_overview.py                     Task 5
├── bin/
│   ├── st-sync                              Task 6  CLI: scan | apply
│   └── st-overview                          Task 7  CLI: render
├── skills/
│   ├── session-sync/SKILL.md                Task 8
│   └── session-overview/SKILL.md            Task 9
├── registry.json                            (runtime, gitignored)
└── harvests/.gitkeep                        Task 0 (Phase 2 uses it)
```

---

### Task 0: Project scaffold

**Files:**
- Create: `.gitignore`, `pytest.ini`, `sessiontracker/__init__.py`, `harvests/.gitkeep`
- Create: dev venv with pytest

- [ ] **Step 1: Create the package and dirs**

```bash
cd ~/wrksp/incubation/session-tracker
mkdir -p sessiontracker tests bin skills harvests docs/plans
touch sessiontracker/__init__.py harvests/.gitkeep
```

- [ ] **Step 2: Write `.gitignore`**

Create `.gitignore`:

```gitignore
# machine-local derived state — never commit
registry.json
# python
.venv/
__pycache__/
*.pyc
.pytest_cache/
```

> Note: `harvests/` IS committed (distilled knowledge we want durable); only `registry.json` is ignored.

- [ ] **Step 3: Write `pytest.ini`**

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
```

- [ ] **Step 4: Create dev venv with pytest**

```bash
cd ~/wrksp/incubation/session-tracker
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip pytest
.venv/bin/pytest --version
```

Expected: prints a `pytest 8.x` version string.

- [ ] **Step 5: Make `bin/` scripts executable later; mark package importable now**

```bash
cd ~/wrksp/incubation/session-tracker
.venv/bin/python -c "import sessiontracker; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add .gitignore pytest.ini sessiontracker/__init__.py harvests/.gitkeep docs/plans/2026-06-07-session-tracker-phase1.md
git commit -m "chore: scaffold session-tracker package and dev tooling"
```

---

### Task 1: `transcript.py` — parse one transcript file

Parses a single `.jsonl` into a `TranscriptInfo`: project `cwd`, titles, last prompt, message count, and a `tail_text` (the last N human/assistant turns) used later for classification.

**Files:**
- Create: `sessiontracker/transcript.py`
- Test: `tests/test_transcript.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_transcript.py`:

```python
import json
from pathlib import Path
from sessiontracker.transcript import parse_transcript, TranscriptInfo


def _write(path: Path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_parse_extracts_metadata_and_tail(tmp_path):
    sid = "11111111-1111-1111-1111-111111111111"
    f = tmp_path / f"{sid}.jsonl"
    _write(f, [
        {"type": "user", "cwd": "/Users/me/proj", "gitBranch": "main",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "cwd": "/Users/me/proj",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "doing it"},
                                 {"type": "tool_use", "name": "Bash"}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "content": "noise"}]}},
        {"type": "user", "message": {"role": "user", "content": "last prompt"}},
        {"type": "custom-title", "customTitle": "my-name", "sessionId": sid},
        {"type": "ai-title", "aiTitle": "auto-name", "sessionId": sid},
        {"type": "last-prompt", "lastPrompt": "last prompt", "sessionId": sid},
        {"type": "summary", "summary": "a summary"},
    ])

    info = parse_transcript(f)

    assert isinstance(info, TranscriptInfo)
    assert info.session_id == sid
    assert info.cwd == "/Users/me/proj"
    assert info.git_branch == "main"
    assert info.custom_title == "my-name"
    assert info.ai_title == "auto-name"
    assert info.summary == "a summary"
    assert info.last_prompt == "last prompt"
    # 4 user/assistant records counted (incl. tool_result-only user msg)
    assert info.message_count == 4
    # tail has the textual turns only, tool_result/tool_use stripped
    assert "USER: first prompt" in info.tail_text
    assert "ASSISTANT: doing it" in info.tail_text
    assert "USER: last prompt" in info.tail_text
    assert "noise" not in info.tail_text


def test_tail_is_limited_to_last_n_turns(tmp_path):
    f = tmp_path / "22222222-2222-2222-2222-222222222222.jsonl"
    recs = [{"type": "user", "message": {"role": "user", "content": f"msg{i}"}}
            for i in range(10)]
    _write(f, recs)
    info = parse_transcript(f, tail_turns=3)
    assert "msg9" in info.tail_text
    assert "msg6" not in info.tail_text


def test_skips_malformed_lines_and_missing_fields(tmp_path):
    f = tmp_path / "33333333-3333-3333-3333-333333333333.jsonl"
    f.write_text(
        'not json\n'
        '{"type":"last-prompt","leafUuid":"x"}\n'      # no lastPrompt key
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8")
    info = parse_transcript(f)
    assert info.message_count == 1
    # falls back to last user turn when no lastPrompt value present
    assert info.last_prompt == "hi"
    assert info.cwd is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_transcript.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.transcript'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/transcript.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TranscriptInfo:
    session_id: str
    cwd: str | None = None
    git_branch: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    summary: str | None = None
    last_prompt: str | None = None
    message_count: int = 0
    tail_text: str = ""


def _text_blocks(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p)
    return ""


def parse_transcript(path, tail_turns: int = 30) -> TranscriptInfo:
    path = Path(path)
    info = TranscriptInfo(session_id=path.stem)
    turns: list[tuple[str, str]] = []

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue

            if info.cwd is None and o.get("cwd"):
                info.cwd = o["cwd"]
            if o.get("gitBranch"):
                info.git_branch = o["gitBranch"]

            t = o.get("type")
            if t == "custom-title":
                info.custom_title = o.get("customTitle") or info.custom_title
            elif t == "ai-title":
                info.ai_title = o.get("aiTitle") or info.ai_title
            elif t == "summary":
                info.summary = o.get("summary") or info.summary
            elif t == "last-prompt":
                info.last_prompt = o.get("lastPrompt") or info.last_prompt
            elif t == "user":
                info.message_count += 1
                txt = _text_blocks(o.get("message", {})).strip()
                if txt:
                    turns.append(("USER", txt))
            elif t == "assistant":
                info.message_count += 1
                txt = _text_blocks(o.get("message", {})).strip()
                if txt:
                    turns.append(("ASSISTANT", txt))

    if not info.last_prompt:
        for speaker, txt in reversed(turns):
            if speaker == "USER":
                info.last_prompt = txt[:500]
                break

    tail = turns[-tail_turns:]
    info.tail_text = "\n".join(f"{s}: {x}" for s, x in tail)
    return info
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_transcript.py -q
```

Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/transcript.py tests/test_transcript.py
git commit -m "feat: parse CC transcript metadata and conversation tail"
```

---

### Task 1b: `gitinfo.py` — resolve a cwd to its git project identity

Resolves a working directory to a stable git-project identity used for grouping:
the normalized `origin` remote slug (preferred), else the repo toplevel, else nothing.
Git calls go through an injectable `runner` so the logic is unit-testable without a repo.

**Files:**
- Create: `sessiontracker/gitinfo.py`
- Test: `tests/test_gitinfo.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gitinfo.py`:

```python
from sessiontracker.gitinfo import normalize_remote, resolve_git_project, GitProject


def test_normalize_remote_variants():
    assert normalize_remote("git@github.com:org/repo.git") == "github.com/org/repo"
    assert normalize_remote("https://github.com/org/repo.git") == "github.com/org/repo"
    assert normalize_remote("ssh://git@gitlab.com/org/repo") == "gitlab.com/org/repo"
    assert normalize_remote("https://github.com/org/repo") == "github.com/org/repo"


def _runner(mapping):
    # mapping: tuple(args after `git -C <cwd>`) -> stdout string, or None to fail
    def run(cmd):
        # cmd = ["git", "-C", cwd, *args]
        key = tuple(cmd[3:])
        return mapping.get(key)
    return run


def test_resolve_prefers_remote_slug(tmp_path):
    cwd = str(tmp_path)  # must exist
    runner = _runner({
        ("rev-parse", "--show-toplevel"): f"{cwd}\n",
        ("config", "--get", "remote.origin.url"): "git@github.com:me/cool-repo.git\n",
    })
    gp = resolve_git_project(cwd, runner=runner)
    assert isinstance(gp, GitProject)
    assert gp.key == "github.com/me/cool-repo"
    assert gp.label == "cool-repo"
    assert gp.root == cwd
    assert gp.remote == "github.com/me/cool-repo"


def test_resolve_falls_back_to_toplevel_without_remote(tmp_path):
    cwd = str(tmp_path)
    runner = _runner({
        ("rev-parse", "--show-toplevel"): f"{cwd}\n",
        ("config", "--get", "remote.origin.url"): None,  # no remote
    })
    gp = resolve_git_project(cwd, runner=runner)
    assert gp.remote is None
    assert gp.key == cwd
    assert gp.label == tmp_path.name


def test_resolve_returns_none_when_not_a_repo(tmp_path):
    cwd = str(tmp_path)
    runner = _runner({("rev-parse", "--show-toplevel"): None})
    assert resolve_git_project(cwd, runner=runner) is None


def test_resolve_returns_none_when_cwd_missing(tmp_path):
    missing = str(tmp_path / "gone")
    # runner should never be consulted; pass one that would explode if called
    def boom(cmd):
        raise AssertionError("runner must not run for a missing cwd")
    assert resolve_git_project(missing, runner=boom) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_gitinfo.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.gitinfo'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/gitinfo.py`:

```python
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitProject:
    root: str | None
    remote: str | None
    key: str        # stable grouping key
    label: str      # human-friendly group label


def normalize_remote(url: str) -> str:
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    if u.startswith("git@"):                       # git@host:org/repo
        u = u[4:].replace(":", "/", 1)
    else:
        for pre in ("https://", "http://", "ssh://", "git://"):
            if u.startswith(pre):
                u = u[len(pre):]
                break
        head, _, tail = u.partition("/")           # strip user@host
        if "@" in head:
            head = head.split("@", 1)[1]
        u = head + ("/" + tail if tail else "")
    return u


def _default_runner(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _git(cwd: str, args: list[str], runner) -> str | None:
    try:
        out = runner(["git", "-C", cwd, *args])
    except Exception:
        return None
    return out.strip() if out else None


def resolve_git_project(cwd: str | None, runner=None) -> GitProject | None:
    if not cwd or not Path(cwd).exists():
        return None
    if runner is None:
        runner = _default_runner
    root = _git(cwd, ["rev-parse", "--show-toplevel"], runner)
    if not root:
        return None
    remote_raw = _git(cwd, ["config", "--get", "remote.origin.url"], runner)
    remote = normalize_remote(remote_raw) if remote_raw else None
    if remote:
        key = remote
        label = remote.rsplit("/", 1)[-1]
    else:
        key = root
        label = Path(root).name
    return GitProject(root=root, remote=remote, key=key, label=label)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_gitinfo.py -q
```

Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/gitinfo.py tests/test_gitinfo.py
git commit -m "feat: resolve cwd to git project identity for grouping"
```

---

### Task 2: `scanner.py` — enumerate sessions into `SessionMeta`

Walks the projects dir, parses each top-level transcript (excluding subagents), and produces `SessionMeta` records carrying file metadata + parsed fields.

**Files:**
- Create: `sessiontracker/scanner.py`
- Test: `tests/test_scanner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scanner.py`:

```python
import json
from pathlib import Path
from sessiontracker.scanner import scan, iter_session_files, SessionMeta


def _session(dir_: Path, sid: str, cwd: str, content="hello"):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd,
                    "message": {"role": "user", "content": content}}) + "\n",
        encoding="utf-8")


def test_iter_excludes_subagents(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "aaaaaaaa-0000-0000-0000-000000000001", "/Users/me/proj")
    # a subagent transcript that must be ignored
    sub = proj / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text("{}\n", encoding="utf-8")

    files = list(iter_session_files(tmp_path))
    assert len(files) == 1
    assert files[0].name == "aaaaaaaa-0000-0000-0000-000000000001.jsonl"


def test_scan_builds_session_meta(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "bbbbbbbb-0000-0000-0000-000000000002", "/Users/me/proj", "do thing")
    metas = scan(tmp_path)
    assert len(metas) == 1
    m = metas[0]
    assert isinstance(m, SessionMeta)
    assert m.session_id == "bbbbbbbb-0000-0000-0000-000000000002"
    assert m.project == "/Users/me/proj"
    assert m.size_bytes > 0
    assert m.mtime.endswith("+00:00")  # tz-aware iso
    assert "do thing" in m.tail_text


def test_scan_missing_root_returns_empty(tmp_path):
    assert scan(tmp_path / "does-not-exist") == []


def test_scan_attaches_injected_git_project(tmp_path):
    from sessiontracker.gitinfo import GitProject
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "cccccccc-0000-0000-0000-000000000009", "/Users/me/proj")

    fake = lambda cwd: GitProject(root="/Users/me/proj",
                                  remote="github.com/me/proj",
                                  key="github.com/me/proj", label="proj")
    metas = scan(tmp_path, git_resolver=fake)
    assert metas[0].git_project_key == "github.com/me/proj"
    assert metas[0].git_project_label == "proj"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_scanner.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.scanner'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/scanner.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .transcript import parse_transcript
from .gitinfo import resolve_git_project


@dataclass
class SessionMeta:
    session_id: str
    file: str
    project: str | None
    title: str | None        # user-set custom title
    ai_title: str | None
    summary: str | None
    last_prompt: str | None
    mtime: str               # tz-aware ISO
    size_bytes: int
    message_count: int
    tail_text: str           # transient; used for classification, not persisted
    git_branch: str | None = None
    git_project_key: str | None = None
    git_project_label: str | None = None
    git_root: str | None = None
    git_remote: str | None = None


def projects_dir() -> Path:
    env = os.environ.get("SESSION_TRACKER_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def iter_session_files(root) -> Iterator[Path]:
    root = Path(root)
    if not root.exists():
        return
    # top-level session files only: <project>/<uuid>.jsonl
    for p in sorted(root.glob("*/*.jsonl")):
        if "subagents" in p.parts:
            continue
        yield p


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()


def scan(root, tail_turns: int = 30, git_resolver=None) -> list[SessionMeta]:
    if git_resolver is None:
        git_resolver = resolve_git_project
    cache: dict[str, object] = {}   # cwd -> GitProject|None, resolved once per cwd
    out: list[SessionMeta] = []
    for p in iter_session_files(root):
        info = parse_transcript(p, tail_turns=tail_turns)
        st = p.stat()
        gp = None
        if info.cwd is not None:
            if info.cwd not in cache:
                cache[info.cwd] = git_resolver(info.cwd)
            gp = cache[info.cwd]
        out.append(SessionMeta(
            session_id=info.session_id,
            file=str(p),
            project=info.cwd,
            title=info.custom_title,
            ai_title=info.ai_title,
            summary=info.summary,
            last_prompt=info.last_prompt,
            mtime=_iso_mtime(p),
            size_bytes=st.st_size,
            message_count=info.message_count,
            tail_text=info.tail_text,
            git_branch=info.git_branch,
            git_project_key=gp.key if gp else None,
            git_project_label=gp.label if gp else None,
            git_root=gp.root if gp else None,
            git_remote=gp.remote if gp else None,
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_scanner.py -q
```

Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/scanner.py tests/test_scanner.py
git commit -m "feat: scan projects dir into session metadata"
```

---

### Task 3: `registry.py` — load/save + hybrid `reconcile`

The registry is the hybrid cache. `reconcile` refreshes metadata for every scanned file, adds new threads as `unclassified`, preserves user decisions, and tombstones vanished sessions.

**Files:**
- Create: `sessiontracker/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry.py`:

```python
from sessiontracker.registry import (
    empty_registry, load_registry, save_registry, reconcile,
)
from sessiontracker.scanner import SessionMeta


def _meta(sid, mtime, project="/p", title=None, git_label=None):
    return SessionMeta(
        session_id=sid, file=f"/x/{sid}.jsonl", project=project,
        title=title, ai_title=None, summary=None, last_prompt="lp",
        mtime=mtime, size_bytes=10, message_count=2, tail_text="USER: hi",
        git_project_key=git_label, git_project_label=git_label,
    )


NOW = "2026-06-07T12:00:00+00:00"


def test_reconcile_adds_new_threads_as_unclassified():
    reg = empty_registry()
    reg2 = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    assert reg2["lastScan"] == NOW
    assert len(reg2["threads"]) == 1
    t = reg2["threads"][0]
    assert t["sessionId"] == "s1"
    assert t["status"] == "unclassified"
    assert t["classifiedAtMtime"] is None
    assert t["decision"] is None


def test_reconcile_refreshes_metadata_but_keeps_classification_and_decision():
    reg = empty_registry()
    reg = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    # simulate a prior classification + user decision
    reg["threads"][0].update(
        status="unfinished", whatItIs="thing", suggestedName="thing-x",
        classifiedAtMtime="2026-06-01T00:00:00+00:00", decision="keep")
    # same session re-scanned with a NEWER mtime + new title
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00", title="named")], NOW)
    t = reg2["threads"][0]
    assert t["mtime"] == "2026-06-05T00:00:00+00:00"   # metadata refreshed
    assert t["title"] == "named"
    assert t["status"] == "unfinished"                 # classification preserved
    assert t["decision"] == "keep"                     # decision preserved
    # mtime now differs from classifiedAtMtime → will need reclassification
    assert t["classifiedAtMtime"] == "2026-06-01T00:00:00+00:00"


def test_reconcile_tombstones_vanished_sessions():
    reg = empty_registry()
    reg = reconcile(reg, [_meta("s1", "2026-06-01T00:00:00+00:00")], NOW)
    reg["threads"][0]["harvestPath"] = "harvests/s1.md"
    # next scan no longer includes s1
    reg2 = reconcile(reg, [], "2026-06-08T00:00:00+00:00")
    assert reg2["threads"] == []
    assert len(reg2["tombstones"]) == 1
    ts = reg2["tombstones"][0]
    assert ts["sessionId"] == "s1"
    assert ts["harvestPath"] == "harvests/s1.md"
    assert ts["deletedAt"] == "2026-06-08T00:00:00+00:00"


def test_reconcile_preserves_git_project_when_cwd_vanishes():
    reg = reconcile(empty_registry(),
                    [_meta("s1", "2026-06-01T00:00:00+00:00", git_label="proj")], NOW)
    assert reg["threads"][0]["gitProjectLabel"] == "proj"
    # next scan can't resolve git (cwd gone) → git fields None, but cached value kept
    reg2 = reconcile(reg, [_meta("s1", "2026-06-05T00:00:00+00:00")], NOW)
    assert reg2["threads"][0]["gitProjectLabel"] == "proj"
    assert reg2["threads"][0]["gitProjectKey"] == "proj"


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "registry.json"
    assert load_registry(p) == empty_registry()   # missing file → empty
    reg = reconcile(empty_registry(), [_meta("s1", NOW)], NOW)
    save_registry(p, reg)
    assert load_registry(p)["threads"][0]["sessionId"] == "s1"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.registry'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/registry.py`:

```python
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

REGISTRY_VERSION = 1

# maps registry key -> SessionMeta field name
_METADATA_FIELDS = {
    "file": "file",
    "project": "project",
    "title": "title",
    "aiTitle": "ai_title",
    "summary": "summary",
    "lastPrompt": "last_prompt",
    "mtime": "mtime",
    "sizeBytes": "size_bytes",
    "messageCount": "message_count",
    "gitBranch": "git_branch",        # from transcript; always available, refresh freely
}

# Git-project identity needs the live filesystem, so a vanished cwd resolves to None.
# Refresh only when the new scan actually resolved a value; otherwise keep the cached one.
_GIT_PROJECT_FIELDS = {
    "gitProjectKey": "git_project_key",
    "gitProjectLabel": "git_project_label",
    "gitRoot": "git_root",
    "gitRemote": "git_remote",
}


def empty_registry() -> dict:
    return {"version": REGISTRY_VERSION, "lastScan": None,
            "threads": [], "tombstones": []}


def load_registry(path) -> dict:
    p = Path(path)
    if not p.exists():
        return empty_registry()
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("lastScan", None)
    data.setdefault("threads", [])
    data.setdefault("tombstones", [])
    return data


def save_registry(path, reg) -> None:
    Path(path).write_text(
        json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def _as_dict(meta) -> dict:
    return asdict(meta) if is_dataclass(meta) else dict(meta)


def _new_thread(md: dict) -> dict:
    return {
        "sessionId": md["session_id"],
        "file": md["file"],
        "project": md["project"],
        "title": md["title"],
        "aiTitle": md["ai_title"],
        "summary": md["summary"],
        "lastPrompt": md["last_prompt"],
        "mtime": md["mtime"],
        "sizeBytes": md["size_bytes"],
        "messageCount": md["message_count"],
        "gitBranch": md["git_branch"],
        "gitProjectKey": md["git_project_key"],
        "gitProjectLabel": md["git_project_label"],
        "gitRoot": md["git_root"],
        "gitRemote": md["git_remote"],
        "status": "unclassified",
        "statusConfidence": 0.0,
        "whatItIs": None,
        "suggestedName": None,
        "classifiedAtMtime": None,
        "decision": None,
        "harvestPath": None,
        "todoId": None,
        "tags": [],
    }


def reconcile(reg: dict, scanned, now_iso: str) -> dict:
    scanned_dicts = [_as_dict(m) for m in scanned]
    by_id = {t["sessionId"]: t for t in reg["threads"]}
    seen = set()
    threads = []

    for md in scanned_dicts:
        sid = md["session_id"]
        seen.add(sid)
        existing = by_id.get(sid)
        if existing is not None:
            for rk, mk in _METADATA_FIELDS.items():
                existing[rk] = md[mk]
            for rk, mk in _GIT_PROJECT_FIELDS.items():
                if md[mk]:                      # keep cached value when cwd is gone
                    existing[rk] = md[mk]
            threads.append(existing)
        else:
            threads.append(_new_thread(md))

    tombstones = list(reg.get("tombstones", []))
    tombstoned = {ts["sessionId"] for ts in tombstones}
    for t in reg["threads"]:
        sid = t["sessionId"]
        if sid not in seen and sid not in tombstoned:
            tombstones.append({
                "sessionId": sid,
                "deletedAt": now_iso,
                "reason": "gc",
                "harvestPath": t.get("harvestPath"),
            })

    return {"version": REGISTRY_VERSION, "lastScan": now_iso,
            "threads": threads, "tombstones": tombstones}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/registry.py tests/test_registry.py
git commit -m "feat: hybrid registry load/save and reconcile"
```

---

### Task 4: `registry.py` — classification helpers

Adds the functions the sync CLI uses to find what needs classifying and to write classifications back.

**Files:**
- Modify: `sessiontracker/registry.py`
- Test: `tests/test_registry.py` (append)

- [ ] **Step 1: Write the failing test (append to `tests/test_registry.py`)**

Append:

```python
from sessiontracker.registry import (
    needs_classification, sessions_needing_classification, apply_classification,
)
import pytest


def _reg_with_one(sid="s1", mtime="2026-06-01T00:00:00+00:00"):
    return reconcile(empty_registry(), [_meta(sid, mtime)], NOW)


def test_new_thread_needs_classification():
    reg = _reg_with_one()
    assert needs_classification(reg["threads"][0]) is True
    assert [t["sessionId"] for t in sessions_needing_classification(reg)] == ["s1"]


def test_apply_classification_marks_done_until_mtime_changes():
    reg = _reg_with_one()
    apply_classification(reg, "s1", status="finished",
                         what_it_is="did the thing", suggested_name="thing",
                         confidence=0.9)
    t = reg["threads"][0]
    assert t["status"] == "finished"
    assert t["whatItIs"] == "did the thing"
    assert t["suggestedName"] == "thing"
    assert t["statusConfidence"] == 0.9
    assert t["classifiedAtMtime"] == t["mtime"]
    assert needs_classification(t) is False
    assert sessions_needing_classification(reg) == []


def test_changed_mtime_reopens_classification():
    reg = _reg_with_one()
    apply_classification(reg, "s1", status="finished",
                         what_it_is="x", suggested_name="x")
    # session touched again → newer mtime
    reg = reconcile(reg, [_meta("s1", "2026-06-09T00:00:00+00:00")], NOW)
    assert needs_classification(reg["threads"][0]) is True


def test_apply_unknown_session_raises():
    reg = _reg_with_one()
    with pytest.raises(KeyError):
        apply_classification(reg, "nope", status="finished",
                             what_it_is="x", suggested_name="x")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: FAIL — `ImportError: cannot import name 'needs_classification'`.

- [ ] **Step 3: Add the implementation to `sessiontracker/registry.py`**

Append:

```python
def needs_classification(thread: dict) -> bool:
    return (thread.get("status") == "unclassified"
            or thread.get("classifiedAtMtime") != thread.get("mtime"))


def sessions_needing_classification(reg: dict) -> list[dict]:
    return [t for t in reg["threads"] if needs_classification(t)]


def apply_classification(reg: dict, session_id: str, *, status: str,
                         what_it_is: str, suggested_name: str,
                         confidence: float | None = None) -> dict:
    for t in reg["threads"]:
        if t["sessionId"] == session_id:
            t["status"] = status
            t["whatItIs"] = what_it_is
            t["suggestedName"] = suggested_name
            if confidence is not None:
                t["statusConfidence"] = confidence
            t["classifiedAtMtime"] = t["mtime"]
            return reg
    raise KeyError(session_id)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_registry.py -q
```

Expected: PASS (9 passed total in this file).

- [ ] **Step 5: Commit**

```bash
git add sessiontracker/registry.py tests/test_registry.py
git commit -m "feat: registry classification helpers"
```

---

### Task 5: `overview.py` — metrics, derived status, render

Computes the spread / WIP-debt metrics, derives `abandoned`, and renders the human-readable overview. Also exposes `is_stale` so the overview CLI can decide whether to nudge a re-sync.

**Files:**
- Create: `sessiontracker/overview.py`
- Test: `tests/test_overview.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_overview.py`:

```python
from sessiontracker.overview import (
    effective_status, compute_metrics, render, is_stale, age_days,
)

NOW = "2026-06-30T00:00:00+00:00"


def _thread(sid, status, mtime, decision=None, project="/p",
            git_key=None, git_label=None):
    return {
        "sessionId": sid, "project": project, "status": status,
        "mtime": mtime, "decision": decision, "whatItIs": f"about {sid}",
        "title": None, "suggestedName": f"{sid}-name", "lastPrompt": "lp",
        "gitProjectKey": git_key, "gitProjectLabel": git_label, "gitBranch": None,
    }


def _reg(threads, last_scan=NOW):
    return {"version": 1, "lastScan": last_scan,
            "threads": threads, "tombstones": []}


def test_age_days():
    assert round(age_days("2026-06-20T00:00:00+00:00", NOW)) == 10


def test_effective_status_derives_abandoned_from_stale_unfinished():
    fresh = _thread("a", "unfinished", "2026-06-29T00:00:00+00:00")
    stale = _thread("b", "unfinished", "2026-06-01T00:00:00+00:00")  # 29d old
    assert effective_status(fresh, NOW) == "unfinished"
    assert effective_status(stale, NOW) == "abandoned"


def test_effective_status_respects_decisions():
    t = _thread("c", "unfinished", NOW, decision="abandon")
    assert effective_status(t, NOW) == "abandoned"
    t2 = _thread("d", "unfinished", NOW, decision="harvested")
    assert effective_status(t2, NOW) == "harvested"


def test_compute_metrics_counts_live_and_spread():
    reg = _reg([
        _thread("a", "unfinished", "2026-06-29T00:00:00+00:00", project="/p1"),
        _thread("b", "blocked", "2026-06-28T00:00:00+00:00", project="/p2"),
        _thread("c", "finished", "2026-06-10T00:00:00+00:00", project="/p1"),
        _thread("d", "unfinished", "2026-06-01T00:00:00+00:00", project="/p3"),  # abandoned
    ])
    m = compute_metrics(reg, NOW)
    assert m["live_count"] == 2            # a + b (c finished, d abandoned)
    assert m["spread_projects"] == 2       # /p1, /p2
    assert m["blocked_count"] == 1
    assert m["by_status"]["abandoned"] == 1
    assert m["by_status"]["finished"] == 1


def test_render_groups_live_threads_and_flags_blocked(tmp_path):
    reg = _reg([
        _thread("a", "blocked", "2026-06-28T00:00:00+00:00", project="/p1"),
        _thread("b", "unfinished", "2026-06-27T00:00:00+00:00", project="/p1"),
    ])
    out = render(reg, NOW)
    assert "/p1" in out
    assert "blocked" in out.lower()
    assert "a-name" in out          # suggestedName shown when no title
    assert "Live threads: 2" in out


def test_render_groups_two_cwds_under_one_git_project():
    # two different working dirs that resolve to the same repo collapse into one group
    reg = _reg([
        _thread("a", "unfinished", "2026-06-29T00:00:00+00:00",
                project="/checkoutA", git_key="github.com/me/repo", git_label="repo"),
        _thread("b", "unfinished", "2026-06-28T00:00:00+00:00",
                project="/checkoutB", git_key="github.com/me/repo", git_label="repo"),
    ])
    out = render(reg, NOW)
    assert out.count("## repo") == 1
    m = compute_metrics(reg, NOW)
    assert m["live_count"] == 2
    assert m["spread_projects"] == 1     # one git project despite two cwds


def test_is_stale_based_on_last_scan():
    assert is_stale(_reg([], last_scan=None), NOW) is True
    assert is_stale(_reg([], last_scan="2026-06-29T23:30:00+00:00"), NOW,
                    max_age_minutes=60) is False
    assert is_stale(_reg([], last_scan="2026-06-20T00:00:00+00:00"), NOW,
                    max_age_minutes=60) is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_overview.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'sessiontracker.overview'`.

- [ ] **Step 3: Write the implementation**

Create `sessiontracker/overview.py`:

```python
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

LIVE_STATUSES = ("active", "unfinished", "blocked")
ABANDON_AGE_DAYS = 21


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def age_days(mtime_iso: str, now_iso: str) -> float:
    return (_parse(now_iso) - _parse(mtime_iso)).total_seconds() / 86400.0


def effective_status(thread: dict, now_iso: str) -> str:
    dec = thread.get("decision")
    if dec == "harvested":
        return "harvested"
    if dec == "abandon":
        return "abandoned"
    st = thread.get("status", "unclassified")
    if st == "unfinished" and age_days(thread["mtime"], now_iso) > ABANDON_AGE_DAYS:
        return "abandoned"
    return st


def group_key(thread: dict) -> str:
    # git identity groups worktrees/clones/monorepo-subdirs together; cwd is the fallback
    return thread.get("gitProjectKey") or thread.get("project") or "(unknown)"


def group_label(thread: dict) -> str:
    return thread.get("gitProjectLabel") or thread.get("project") or "(unknown)"


def _live(reg: dict, now_iso: str) -> list[dict]:
    return [t for t in reg["threads"]
            if effective_status(t, now_iso) in LIVE_STATUSES]


def compute_metrics(reg: dict, now_iso: str) -> dict:
    eff = [(t, effective_status(t, now_iso)) for t in reg["threads"]]
    by_status = Counter(es for _, es in eff)
    live = [t for t, es in eff if es in LIVE_STATUSES]
    by_project = Counter(group_key(t) for t in live)
    blocked = [t for t, es in eff if es == "blocked"]
    stale = [t for t in live if age_days(t["mtime"], now_iso) > ABANDON_AGE_DAYS]
    oldest = max(live, key=lambda t: age_days(t["mtime"], now_iso), default=None)
    return {
        "by_status": dict(by_status),
        "live_count": len(live),
        "spread_projects": len(by_project),
        "by_project": dict(by_project),
        "blocked_count": len(blocked),
        "stale_count": len(stale),
        "oldest_age_days": round(age_days(oldest["mtime"], now_iso), 1) if oldest else None,
        "unclassified": by_status.get("unclassified", 0),
    }


def _label(thread: dict) -> str:
    return thread.get("title") or thread.get("suggestedName") or thread["sessionId"][:8]


def render(reg: dict, now_iso: str) -> str:
    m = compute_metrics(reg, now_iso)
    lines = []
    lines.append("=== session-tracker overview ===")
    lines.append(
        f"Live threads: {m['live_count']}  |  "
        f"spread across {m['spread_projects']} projects  |  "
        f"blocked: {m['blocked_count']}  |  stale(>21d): {m['stale_count']}")
    if m["oldest_age_days"] is not None:
        lines.append(f"Oldest live thread: {m['oldest_age_days']} days untouched")
    if m["unclassified"]:
        lines.append(f"(! {m['unclassified']} unclassified — run session-sync)")
    lines.append("")

    # group live threads by git project (fallback: cwd)
    groups: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for t in reg["threads"]:
        es = effective_status(t, now_iso)
        if es in LIVE_STATUSES:
            k = group_key(t)
            groups[k].append((t, es))
            labels.setdefault(k, group_label(t))

    for key in sorted(groups, key=lambda k: labels[k].lower()):
        lines.append(f"## {labels[key]}")
        items = sorted(groups[key],
                       key=lambda pair: age_days(pair[0]["mtime"], now_iso))
        for t, es in items:
            age = round(age_days(t["mtime"], now_iso))
            flag = "  <- BLOCKED" if es == "blocked" else ""
            branch = f" @{t['gitBranch']}" if t.get("gitBranch") else ""
            lines.append(
                f"  [{es}]{branch} {_label(t)} ({age}d) — {t.get('whatItIs') or ''}{flag}")
        lines.append("")

    # status breakdown footer
    breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(m["by_status"].items()))
    lines.append(f"All sessions by status: {breakdown}")
    return "\n".join(lines)


def registry_age_minutes(reg: dict, now_iso: str) -> float | None:
    ls = reg.get("lastScan")
    if not ls:
        return None
    return (_parse(now_iso) - _parse(ls)).total_seconds() / 60.0


def is_stale(reg: dict, now_iso: str, max_age_minutes: float = 60) -> bool:
    age = registry_age_minutes(reg, now_iso)
    return age is None or age > max_age_minutes
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_overview.py -q
```

Expected: PASS (7 passed).

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS (all files; ~28 tests).

- [ ] **Step 6: Commit**

```bash
git add sessiontracker/overview.py tests/test_overview.py
git commit -m "feat: overview metrics, derived status, and rendering"
```

---

### Task 6: `bin/st-sync` CLI (scan + apply)

Thin entrypoint. `scan` reconciles the registry and emits the sessions needing classification (with tails) as JSON. `apply` writes one classification back.

**Files:**
- Create: `bin/st-sync`

- [ ] **Step 1: Write the implementation**

Create `bin/st-sync`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import scanner, registry  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_scan(args):
    reg = registry.load_registry(args.registry)
    scanned = scanner.scan(scanner.projects_dir(), tail_turns=args.tail)
    tails = {s.session_id: s.tail_text for s in scanned}
    reg = registry.reconcile(reg, scanned, _now())
    registry.save_registry(args.registry, reg)

    pending = registry.sessions_needing_classification(reg)
    emit = [{
        "sessionId": t["sessionId"],
        "project": t["project"],
        "gitProject": t["gitProjectLabel"],
        "gitBranch": t["gitBranch"],
        "customTitle": t["title"],
        "aiTitle": t["aiTitle"],
        "lastPrompt": t["lastPrompt"],
        "messageCount": t["messageCount"],
        "tail": tails.get(t["sessionId"], "")[:6000],
    } for t in pending]
    json.dump({"pendingCount": len(emit), "total": len(reg["threads"]),
               "pending": emit}, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_apply(args):
    reg = registry.load_registry(args.registry)
    registry.apply_classification(
        reg, args.session, status=args.status, what_it_is=args.what,
        suggested_name=args.name, confidence=args.confidence)
    registry.save_registry(args.registry, reg)
    print(f"applied {args.session}: {args.status}")


def main():
    ap = argparse.ArgumentParser(prog="st-sync")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = ap.add_subparsers(required=True, dest="cmd")

    s = sub.add_parser("scan", help="reconcile registry; emit pending classifications")
    s.add_argument("--tail", type=int, default=30)
    s.set_defaults(fn=cmd_scan)

    a = sub.add_parser("apply", help="write one classification back")
    a.add_argument("--session", required=True)
    a.add_argument("--status", required=True,
                   choices=["active", "unfinished", "blocked", "finished"])
    a.add_argument("--what", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--confidence", type=float, default=None)
    a.set_defaults(fn=cmd_apply)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x bin/st-sync
```

- [ ] **Step 3: Smoke-test `scan` against a synthetic projects dir (no real data touched)**

```bash
cd ~/wrksp/incubation/session-tracker
TMP=$(mktemp -d)
mkdir -p "$TMP/-Users-me-proj"
printf '%s\n' '{"type":"user","cwd":"/Users/me/proj","message":{"role":"user","content":"please add a login button and we are not done yet"}}' > "$TMP/-Users-me-proj/cccccccc-0000-0000-0000-000000000003.jsonl"
SESSION_TRACKER_PROJECTS_DIR="$TMP" .venv/bin/python bin/st-sync --registry "$TMP/registry.json" scan
```

Expected: JSON with `pendingCount: 1`, one `pending` entry whose `tail` contains "login button".

- [ ] **Step 4: Smoke-test `apply`**

```bash
.venv/bin/python bin/st-sync --registry "$TMP/registry.json" apply \
  --session cccccccc-0000-0000-0000-000000000003 \
  --status unfinished --what "add a login button" --name "login-button" --confidence 0.8
SESSION_TRACKER_PROJECTS_DIR="$TMP" .venv/bin/python bin/st-sync --registry "$TMP/registry.json" scan
```

Expected: first command prints `applied …: unfinished`; second shows `pendingCount: 0` (mtime unchanged → already classified).

- [ ] **Step 5: Clean up temp + commit**

```bash
rm -rf "$TMP"
git add bin/st-sync
git commit -m "feat: st-sync CLI (scan and apply)"
```

---

### Task 7: `bin/st-overview` CLI

Renders the overview from the registry. Prints a nudge to run sync if the registry is stale or has unclassified threads.

**Files:**
- Create: `bin/st-overview`

- [ ] **Step 1: Write the implementation**

Create `bin/st-overview`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sessiontracker import registry, overview  # noqa: E402

DEFAULT_REGISTRY = REPO / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(prog="st-overview")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args()

    now = _now()
    reg = registry.load_registry(args.registry)
    if overview.is_stale(reg, now):
        print("(registry is stale — run the session-sync skill to refresh)\n")
    print(overview.render(reg, now))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable + smoke-test**

```bash
cd ~/wrksp/incubation/session-tracker
chmod +x bin/st-overview
TMP=$(mktemp -d)
mkdir -p "$TMP/-Users-me-proj"
printf '%s\n' '{"type":"user","cwd":"/Users/me/proj","message":{"role":"user","content":"do a thing"}}' > "$TMP/-Users-me-proj/dddddddd-0000-0000-0000-000000000004.jsonl"
SESSION_TRACKER_PROJECTS_DIR="$TMP" .venv/bin/python bin/st-sync --registry "$TMP/registry.json" scan >/dev/null
.venv/bin/python bin/st-sync --registry "$TMP/registry.json" apply --session dddddddd-0000-0000-0000-000000000004 --status blocked --what "waiting on me" --name "do-a-thing"
.venv/bin/python bin/st-overview --registry "$TMP/registry.json"
```

Expected: overview text with `Live threads: 1`, a `## /Users/me/proj` group, and the thread flagged `<- BLOCKED`.

- [ ] **Step 3: Clean up + commit**

```bash
rm -rf "$TMP"
git add bin/st-overview
git commit -m "feat: st-overview CLI"
```

---

### Task 8: `session-sync` skill

The skill that drives classification: run `st-sync scan`, classify each pending session from its tail, write results back with `st-sync apply`.

**Files:**
- Create: `skills/session-sync/SKILL.md`

- [ ] **Step 1: Write the skill**

Create `skills/session-sync/SKILL.md`:

```markdown
---
name: session-sync
description: Use to refresh the session-tracker registry — scan ~/.claude/projects, then classify each dormant Claude Code session that is new or changed since last sync. Run before session-overview, or whenever the overview reports unclassified threads.
---

# session-sync

Refreshes `registry.json` and classifies dormant sessions. Deterministic scanning is
done by the `st-sync` CLI; you (the agent) do the classification.

## Steps

1. **Scan and reconcile.** From the repo root
   (`~/wrksp/incubation/session-tracker`), run:

   ```bash
   .venv/bin/python bin/st-sync scan
   ```

   This rewrites `registry.json` and prints JSON: `{pendingCount, total, pending: [...]}`.
   Each `pending` item has `sessionId`, `project`, `customTitle`, `aiTitle`,
   `lastPrompt`, `messageCount`, and `tail` (the last turns of the conversation).

2. **If `pendingCount` is 0**, report "registry up to date (N sessions)" and stop.

3. **Classify each pending session** from its `tail` (and `lastPrompt`). Decide:
   - `status` — exactly one of:
     - `finished` — the work reached a clean conclusion (task done, tests pass,
       question answered, user said thanks / it works).
     - `blocked` — the last turn is a question or error waiting on the user, or the
       assistant asked for a decision and nothing followed.
     - `unfinished` — real work was underway and just stops mid-stream, no clean end.
     - `active` — only if it looks like it was interrupted moments ago (rare for
       dormant sessions); when unsure between active and unfinished, pick `unfinished`.
   - `whatItIs` — one concise line describing the work (≤ ~90 chars).
   - `suggestedName` — kebab-case, ≤ ~40 chars, specific (e.g. `login-button-anim`,
     not `frontend-work`). If `customTitle` is already set, reuse it as the name.
   - `confidence` — 0.0–1.0.

   For large backfills (dozens of pending sessions on the first run), classify in
   batches and consider dispatching parallel subagents (one per batch) to keep it fast.

4. **Write each result back:**

   ```bash
   .venv/bin/python bin/st-sync apply --session <sessionId> \
     --status <status> --what "<whatItIs>" --name "<suggestedName>" --confidence <0..1>
   ```

5. **Report** a short summary: how many classified, and the status breakdown.

## Notes
- Never hand-edit `registry.json` or the `.jsonl` transcripts.
- `suggestedName` is a *proposal*. Applying it to the real session is the
  `session-name` skill's job (Phase 2) via native `/rename` / `claude -n`.
- Re-running is cheap: only new or changed (newer `mtime`) sessions reappear as pending.
```

- [ ] **Step 2: Verify the skill's commands work end-to-end against a temp dir**

```bash
cd ~/wrksp/incubation/session-tracker
TMP=$(mktemp -d)
mkdir -p "$TMP/-Users-me-proj"
printf '%s\n' '{"type":"user","cwd":"/Users/me/proj","message":{"role":"user","content":"add login button, not done"}}' > "$TMP/-Users-me-proj/eeeeeeee-0000-0000-0000-000000000005.jsonl"
SESSION_TRACKER_PROJECTS_DIR="$TMP" .venv/bin/python bin/st-sync --registry "$TMP/registry.json" scan | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print('pending',d['pendingCount'])"
rm -rf "$TMP"
```

Expected: prints `pending 1`.

- [ ] **Step 3: Commit**

```bash
git add skills/session-sync/SKILL.md
git commit -m "feat: session-sync skill"
```

---

### Task 9: `session-overview` skill

**Files:**
- Create: `skills/session-overview/SKILL.md`

- [ ] **Step 1: Write the skill**

Create `skills/session-overview/SKILL.md`:

```markdown
---
name: session-overview
description: Use to see all dormant Claude Code work threads across every project at a glance — what each is about, its status, age, and how thin you are spread (live thread count, project spread, blocked and stale counts). Answers "what do I have in flight / abandoned right now?"
---

# session-overview

Shows the curated overview of dormant sessions from the session-tracker registry.

## Steps

1. **Check freshness.** From the repo root (`~/wrksp/incubation/session-tracker`), run:

   ```bash
   .venv/bin/python bin/st-overview
   ```

2. **If the output starts with a staleness/unclassified nudge**, run the
   `session-sync` skill first, then re-run the command above.

3. **Present the overview to the user**, then add a short read of the situation:
   - Call out anything `blocked` (waiting on them).
   - Call out stale live threads (>21 days) as abandon/harvest candidates.
   - If `Live threads` is high and spread across many projects, note the spread
     ("you're juggling N threads across M projects").

## Notes
- This is read-only; it never deletes or renames anything.
- Curation actions (naming, harvest, prune) are separate Phase-2 skills.
```

- [ ] **Step 2: Commit**

```bash
git add skills/session-overview/SKILL.md
git commit -m "feat: session-overview skill"
```

---

### Task 10: End-to-end dry run against real data (read-only)

Validate the whole pipeline against the real `~/.claude/projects` once, writing only to a throwaway registry path (never the default), then review output by hand.

**Files:** none (validation only)

- [ ] **Step 1: Run a real scan to a throwaway registry**

```bash
cd ~/wrksp/incubation/session-tracker
.venv/bin/python bin/st-sync --registry /tmp/st-real-registry.json scan \
  | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin);print('pending',d['pendingCount'],'total',d['total'])"
```

Expected: `pending` ≈ `total` ≈ ~219 (first run; everything unclassified). No errors, no traceback.

- [ ] **Step 2: Sanity-check parsing quality**

```bash
.venv/bin/python -c "
import json
d=json.load(open('/tmp/st-real-registry.json'))
ts=d['threads']
print('threads:',len(ts))
print('with project(cwd):',sum(1 for t in ts if t['project']))
print('with git project:',sum(1 for t in ts if t['gitProjectKey']))
print('distinct git projects:',len({t['gitProjectKey'] for t in ts if t['gitProjectKey']}))
print('distinct cwds:',len({t['project'] for t in ts if t['project']}))
print('user-named:',sum(1 for t in ts if t['title']))
print('ai-titled:',sum(1 for t in ts if t['aiTitle']))
print('sample:',[ (t['gitProjectLabel'] or t['project'], t['title'] or t['aiTitle']) for t in ts[:5] ])
"
```

Expected: `threads` ~219; `with project(cwd)` is the large majority; `distinct git projects` is meaningfully **smaller** than `distinct cwds` (worktrees/clones collapsing — e.g. the `acme-core` siblings); named/ai-titled counts plausible; samples show real repo labels.

- [ ] **Step 3: Classify a few by hand to validate `apply` + overview on real data**

Pick 2–3 sessionIds from the registry, run `st-sync apply` for each with a sensible status, then:

```bash
.venv/bin/python bin/st-overview --registry /tmp/st-real-registry.json | head -40
```

Expected: the overview renders real projects and the threads you just classified, with correct ages and any `BLOCKED` flag.

- [ ] **Step 4: Clean up the throwaway registry**

```bash
rm -f /tmp/st-real-registry.json
```

- [ ] **Step 5: Final full test run + commit a checkpoint note**

```bash
.venv/bin/pytest -q
```

Expected: all green. (No code to commit here unless Step 2/3 surfaced parsing fixes — if they did, fix in the relevant module + add a regression test before committing.)

---

## Self-review against SPEC.md

- **Hybrid registry (C):** Tasks 3–4 — reconcile refreshes metadata, preserves decisions, tombstones vanished; `needs_classification` keyed on `mtime != classifiedAtMtime`. ✓
- **Lifecycle classifier (①):** Task 8 skill defines the rubric; `active|unfinished|blocked|finished` returned, `abandoned` derived in Task 5. ✓
- **Overview + spread/WIP (#1, ④):** Task 5 `compute_metrics` (live_count, spread_projects, blocked, stale, oldest) + `render`. ✓
- **Project path from `cwd`, not dir name:** Task 1 `parse_transcript`. ✓
- **Grouping by git project (not just cwd):** Task 1b `gitinfo.py` resolves cwd→repo identity (remote slug → toplevel → cwd); Task 2 scanner attaches + caches per cwd; Task 3 registry carries and *preserves* git fields when a cwd vanishes; Task 5 `render`/`compute_metrics` group + measure spread by git project, with `gitBranch` shown per thread. ✓
- **custom-title vs ai-title:** captured in Tasks 1–3; "named" = has `title`. ✓
- **No hand-editing `.jsonl`:** naming is a *proposal* only; noted in Task 8. ✓
- **Homes/layout:** matches SPEC (`sessiontracker/`, `bin/`, `skills/`, `registry.json` gitignored, `harvests/` committed). ✓
- **`.gitignore` policy:** Task 0 ignores `registry.json`, commits `harvests/`. ✓
- **`/clear` boundaries:** explicitly deferred (documented above). ✓ (SPEC noted as refinement.)
- **Out of scope for Phase 1 (correctly absent):** naming application, harvest, prune, todo linking, `cleanupPeriodDays` change (already done by user). ✓
```
