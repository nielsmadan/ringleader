# Harvest + Complete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `rl session harvest` (distill a session's durable knowledge into a stored markdown harvest via the C1 Model seam), `rl harvests` (list the store), and `rl session complete` (= harvest + archive).

**Architecture:** `harvest` feeds the model a condensed render of the session (`export.render_conversation`, which already strips tool output), gets a structured distillation back via the C1 `Model` seam, renders markdown, and writes it to a central store under `state_dir()/harvests/` as a frontmatter file tagged with the session's repo. `complete` chains harvest → archive. No repo-doc integration — ringleader stores; the skills layer integrates.

**Tech Stack:** Python ≥3.13, stdlib; the C1 Model seam (`ringleader/models/`) + `config`; `pytest`, `ruff`.

## Global Constraints

- **Reuse C1, don't rebuild.** `config.load_config()`, `models.active_model(cfg)`, `Model.complete_json(prompt, schema)`, `models.base.ModelError` — mirror `ringleader/commands/enrich.py`.
- **Harvest input is the condensed render.** Use `export.render_conversation(file)` — it already keeps only user/assistant text + a tools note and drops tool-result bodies. No `--full` mode (YAGNI; no full renderer exists and condensed is the right, ~10× cheaper input).
- **ringleader stores no secrets; no new required runtime dependency** (`pyproject.toml` `dependencies` stays `[]`).
- **Backend-agnostic + graceful degradation:** `harvest`/`complete` ask the model only for structured JSON; no model available → no-op with the enrich-style guidance message (exit 0); `complete` archives nothing when harvest can't run.
- **Central store:** `state_dir()/harvests/`, one markdown file per harvest with YAML frontmatter (`sessionId`, `name`, `folderPath`, `repoUrl`, `harvestedAt`) + the distilled markdown body.
- **`complete` implements the reserved `session complete` stub** (`_reserved("session complete")` in `commands/__init__.py`; `"session complete"` in `cli.py` `_RESERVED`).
- **Tests never call a real model or scan real data:** mock the `Model` seam; isolate `state_dir` via `XDG_DATA_HOME=tmp`; use a tmp `--registry`.
- Tests pass with `uv run pytest -q`; `uv run ruff check` and `uv run ruff format --check` clean.

## Reference: verified current interfaces

- `export.render_conversation(path) -> str` (`ringleader/export.py`) — Claude-only today: iterates jsonl, emits `## USER`/`## ASSISTANT` text + `_(tools: …)_`, drops tool results. Uses `transcript._text_blocks(message)` (extracts `type=="text"` content blocks).
- `registry.mark_harvested(reg, session_id, harvest_paths) -> dict` (registry.py:201) — sets `decision="harvested"`, `harvestPath=harvest_paths or None`.
- `registry.archive_session(reg, session_id, archive_path, archived_at, ...)` (registry.py:221); `commands/archive.py::cmd_archive` does the file move (transcript + sidecar + optional summary) then calls it.
- Registry thread fields: `sessionId`, `file`, `project` (cwd), `gitRoot`, `gitRemote`, `gitProjectLabel`, `title`, `suggestedName`, `source`.
- `paths.state_dir() -> Path`; `config.load_config()`; `models.active_model(cfg)`.
- `memory.parse_frontmatter(text) -> tuple[dict, str]` (`ringleader/memory.py`) — parses YAML-ish frontmatter + body (reuse for reading harvests).
- Codex transcript lines: `{"type":"response_item","payload":{"type":"message","role":"user"|"assistant","content":[{"text":…}]}}`; tool calls are `payload.type=="function_call"`.
- Reserved stub: `_SESSION["complete"] = _reserved("session complete")` in `commands/__init__.py`; dispatch tests in `tests/test_rl_dispatch.py`.

## File Structure

- `ringleader/export.py` — **modify.** Add a Codex branch to `render_conversation`.
- `ringleader/harvests.py` — **create.** Store: `Harvest` dataclass, `harvests_dir`, `write_harvest`, `load_harvests`.
- `ringleader/commands/harvest.py` — **create.** `cmd_harvest` (`rl session harvest`).
- `ringleader/commands/harvests.py` — **create.** `cmd_harvests` (`rl harvests`).
- `ringleader/commands/complete.py` — **create.** `cmd_complete` (`rl session complete`).
- `ringleader/commands/archive.py` — **modify.** Extract an `archive_thread(...)` helper reused by `complete`.
- `ringleader/commands/__init__.py`, `ringleader/cli.py` — **modify.** Wire `session harvest`, top-level `harvests`, `session complete` (de-reserve `complete`).
- `README.md`, `AGENTS.md` — **modify.** Document the three commands.
- Tests: `tests/test_export.py` (modify), `tests/test_harvests.py`, `tests/test_rl_harvest.py`, `tests/test_rl_harvests.py`, `tests/test_rl_complete.py`, `tests/test_rl_dispatch.py` (modify) — **create/modify**.

---

### Task 1: Codex support in the condensed render

**Files:**
- Modify: `ringleader/export.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Produces: `export.render_conversation(path)` now also renders Codex rollout transcripts (user/assistant `response_item` messages) to the same `## USER`/`## ASSISTANT` markdown, still dropping tool-result bodies.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_export.py`:

```python
import json

from ringleader import export


def test_render_conversation_handles_codex(tmp_path):
    f = tmp_path / "rollout.jsonl"
    f.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"type": "session_meta", "payload": {"session_id": "s", "cwd": "/p"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "do the thing"}]}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "done the thing"}]}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = export.render_conversation(f)
    assert "## USER" in out and "do the thing" in out
    assert "## ASSISTANT" in out and "done the thing" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_export.py -q`
Expected: FAIL (Codex lines produce no output).

- [ ] **Step 3: Add the Codex branch**

In `ringleader/export.py` `render_conversation`, after the existing `assistant` branch, add:

```python
            elif t == "response_item":
                p = o.get("payload", {})
                if p.get("type") == "message":
                    role = p.get("role")
                    txt = " ".join(
                        c.get("text", "")
                        for c in p.get("content", [])
                        if isinstance(c, dict)
                    ).strip()
                    if txt and role == "user":
                        out.append(f"## USER\n\n{txt}\n")
                    elif txt and role == "assistant":
                        out.append(f"## ASSISTANT\n\n{txt}\n")
```

(Tool `function_call` payloads are skipped — condensed, mirroring how Claude tool results are dropped.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_export.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ringleader/export.py tests/test_export.py
git commit -m "feat: render codex transcripts in the condensed conversation view"
```

---

### Task 2: Harvest store module

**Files:**
- Create: `ringleader/harvests.py`
- Test: `tests/test_harvests.py`

**Interfaces:**
- Produces:
  - `harvests.harvests_dir() -> Path` (`state_dir()/harvests`).
  - `harvests.Harvest` dataclass: `path: Path`, `session_id`, `name`, `folder_path`, `repo_url`, `harvested_at`, `content: str`.
  - `harvests.write_harvest(*, session_id, name, folder_path, repo_url, harvested_at, content) -> Path` — writes `<harvests_dir>/<slug>.md` with YAML frontmatter + `content` body; returns the path. Slug from `name`/`session_id`.
  - `harvests.load_harvests() -> list[Harvest]` — parse every `*.md` in `harvests_dir()`, newest first.

- [ ] **Step 1: Write the failing tests**

`tests/test_harvests.py`:

```python
def test_write_and_load_harvest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests

    p = harvests.write_harvest(
        session_id="s1", name="fix-auth", folder_path="/repo",
        repo_url="git@github.com:me/repo.git", harvested_at="2026-01-01T00:00:00+00:00",
        content="## Summary\n\ndid the auth fix\n",
    )
    assert p.exists() and p.parent == harvests.harvests_dir()
    got = harvests.load_harvests()
    assert len(got) == 1
    h = got[0]
    assert h.session_id == "s1" and h.repo_url == "git@github.com:me/repo.git"
    assert h.folder_path == "/repo" and "did the auth fix" in h.content


def test_load_empty_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests

    assert harvests.load_harvests() == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_harvests.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `harvests.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .memory import parse_frontmatter
from .paths import state_dir


def harvests_dir() -> Path:
    return state_dir() / "harvests"


@dataclass
class Harvest:
    path: Path
    session_id: str
    name: str
    folder_path: str | None
    repo_url: str | None
    harvested_at: str | None
    content: str


def _slug(name: str, session_id: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", (name or session_id).lower()).strip("-") or "harvest"
    return f"{base}-{session_id[:8]}"


def write_harvest(*, session_id, name, folder_path, repo_url, harvested_at, content) -> Path:
    d = harvests_dir()
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"sessionId: {session_id}\n"
        f"name: {name or ''}\n"
        f"folderPath: {folder_path or ''}\n"
        f"repoUrl: {repo_url or ''}\n"
        f"harvestedAt: {harvested_at or ''}\n"
        "---\n\n"
    )
    p = d / f"{_slug(name or '', session_id)}.md"
    p.write_text(fm + content.rstrip() + "\n", encoding="utf-8")
    return p


def load_harvests() -> list[Harvest]:
    d = harvests_dir()
    if not d.exists():
        return []
    out: list[Harvest] = []
    for f in d.glob("*.md"):
        meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        out.append(
            Harvest(
                path=f,
                session_id=meta.get("sessionId", ""),
                name=meta.get("name", ""),
                folder_path=meta.get("folderPath") or None,
                repo_url=meta.get("repoUrl") or None,
                harvested_at=meta.get("harvestedAt") or None,
                content=body.strip(),
            )
        )
    return sorted(out, key=lambda h: h.harvested_at or "", reverse=True)
```

(If `memory.parse_frontmatter` doesn't cleanly parse this frontmatter, add a tiny local parser instead — verify against a written file during Step 4.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_harvests.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ringleader/harvests.py tests/test_harvests.py
git commit -m "feat: add the harvest store"
```

---

### Task 3: `rl session harvest`

**Files:**
- Create: `ringleader/commands/harvest.py`
- Modify: `ringleader/commands/__init__.py`, `ringleader/cli.py` (only if needed for wiring)
- Test: `tests/test_rl_harvest.py`

**Interfaces:**
- Consumes: `export.render_conversation`, `models.active_model`, `config.load_config`, `harvests.write_harvest`, `registry.mark_harvested`.
- Produces: `commands.harvest.cmd_harvest(argv) -> int`; dispatch entry `_SESSION["harvest"]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_rl_harvest.py` — call `cmd_harvest` directly, mock the model + render:

```python
import json

from ringleader.commands import harvest as harvest_mod
from ringleader.models.base import ModelError


class FakeModel:
    name = "fake"

    def __init__(self, out=None, boom=False):
        self._out = out or {"summary": "did the thing", "gotchas": ["watch the cache"]}
        self._boom = boom

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        if self._boom:
            raise ModelError("boom")
        return self._out


def _seed(reg_path, tmp_path):
    f = tmp_path / "s1.jsonl"
    f.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    reg_path.write_text(json.dumps({"version": 2, "threads": [
        {"sessionId": "s1", "file": str(f), "project": "/repo", "gitRoot": "/repo",
         "gitRemote": "git@github.com:me/repo.git", "title": "fix-auth"}], "tombstones": []}),
        encoding="utf-8")


def test_harvest_writes_store_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"; _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(harvest_mod.export, "render_conversation", lambda p: "## USER\n\nhi\n")
    rc = harvest_mod.cmd_harvest(["s1", "--registry", str(reg)])
    assert rc == 0
    from ringleader import harvests
    got = harvests.load_harvests()
    assert len(got) == 1 and "did the thing" in got[0].content
    assert got[0].repo_url == "git@github.com:me/repo.git"
    # harvested flag set
    assert json.loads(reg.read_text())["threads"][0]["decision"] == "harvested"


def test_harvest_no_model_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"; _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: None)
    rc = harvest_mod.cmd_harvest(["s1", "--registry", str(reg)])
    assert rc == 0 and "no model configured" in capsys.readouterr().out.lower()
    from ringleader import harvests
    assert harvests.load_harvests() == []


def test_harvest_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"; _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(harvest_mod.export, "render_conversation", lambda p: "hi")
    rc = harvest_mod.cmd_harvest(["s1", "--dry-run", "--registry", str(reg)])
    assert rc == 0
    from ringleader import harvests
    assert harvests.load_harvests() == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_rl_harvest.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `commands/harvest.py`**

```python
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .. import export, harvests, registry
from ..config import load_config
from ..models import active_model
from ..models.base import ModelError
from . import default_registry

HARVEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "gotchas": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "whatChanged": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prompt(thread: dict, condensed: str) -> str:
    return (
        "Extract the DURABLE, reusable knowledge from this finished coding session for the "
        "repo's docs. Keep only what's worth remembering later; discard routine steps.\n"
        f"project: {thread.get('project')}\n\n"
        "Condensed transcript:\n"
        f"{condensed[:24000]}\n\n"
        "Return: summary (one paragraph), gotchas (non-obvious things), decisions "
        "(+rationale), whatChanged."
    )


def _render_md(thread: dict, data: dict) -> str:
    title = thread.get("title") or thread.get("suggestedName") or thread["sessionId"]
    parts = [f"# {title}\n", f"## Summary\n\n{data.get('summary', '').strip()}\n"]
    for key, heading in (("gotchas", "Gotchas"), ("decisions", "Decisions"),
                         ("whatChanged", "What changed")):
        items = [i for i in (data.get(key) or []) if i]
        if items:
            parts.append(f"## {heading}\n\n" + "\n".join(f"- {i}" for i in items) + "\n")
    return "\n".join(parts)


def cmd_harvest(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session harvest")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    model = active_model(load_config())
    if model is None:
        print("no model configured — set one with `rl config`, or harvest via the triage/cleanup skill")
        return 0

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None:
        print(f"rl-harvest: unknown session {args.id}", file=sys.stderr)
        return 1

    condensed = export.render_conversation(t["file"])
    try:
        data = model.complete_json(_prompt(t, condensed), HARVEST_SCHEMA)
    except (ModelError, KeyError) as e:
        print(f"rl-harvest: {e}", file=sys.stderr)
        return 1
    md = _render_md(t, data)

    if args.dry_run:
        sys.stdout.write(md + "\n")
        return 0

    path = harvests.write_harvest(
        session_id=t["sessionId"],
        name=t.get("title") or t.get("suggestedName") or "",
        folder_path=t.get("gitRoot") or t.get("project"),
        repo_url=t.get("gitRemote"),
        harvested_at=_now(),
        content=md,
    )
    registry.mark_harvested(reg, t["sessionId"], [str(path)])
    registry.save_registry(args.registry, reg)
    print(f"harvested {t['sessionId']} -> {path}")
    return 0
```

- [ ] **Step 4: Wire the dispatch**

In `ringleader/commands/__init__.py`, import `harvest` and set `_SESSION["harvest"] = harvest.cmd_harvest` (place it near `enrich`).

- [ ] **Step 5: Run tests + ruff**

Run: `uv run pytest tests/test_rl_harvest.py -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add ringleader/commands/harvest.py ringleader/commands/__init__.py tests/test_rl_harvest.py
git commit -m "feat: add rl session harvest"
```

---

### Task 4: `rl harvests`

**Files:**
- Create: `ringleader/commands/harvests.py`
- Modify: `ringleader/commands/__init__.py`, `ringleader/cli.py` (add `harvests` to the flat verbs)
- Test: `tests/test_rl_harvests.py`, `tests/test_rl_dispatch.py`

**Interfaces:**
- Consumes: `harvests.load_harvests`.
- Produces: `commands.harvests.cmd_harvests(argv) -> int`; top-level dispatch entry `harvests`.

- [ ] **Step 1: Write the failing tests**

`tests/test_rl_harvests.py`:

```python
def test_harvests_lists_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd

    harvests.write_harvest(session_id="s1", name="fix-auth", folder_path="/repo",
                           repo_url="git@github.com:me/repo.git",
                           harvested_at="2026-01-01T00:00:00+00:00", content="## Summary\n\nx\n")
    rc = cmd.cmd_harvests([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fix-auth" in out and "git@github.com:me/repo.git" in out and "/repo" in out


def test_harvests_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd
    import json

    harvests.write_harvest(session_id="s1", name="n", folder_path="/r", repo_url="u",
                           harvested_at="t", content="body text")
    rc = cmd.cmd_harvests(["--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["repoUrl"] == "u" and "body text" in data[0]["content"]


def test_harvests_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader.commands import harvests as cmd
    assert cmd.cmd_harvests([]) == 0
```

Add to `tests/test_rl_dispatch.py` `test_new_paths_resolve`: `(["harvests"], commands.harvests.cmd_harvests)`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_rl_harvests.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement `commands/harvests.py`**

```python
from __future__ import annotations

import argparse
import json
import sys

from .. import harvests as store


def cmd_harvests(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = store.load_harvests()
    if args.json:
        json.dump(
            [
                {"sessionId": h.session_id, "name": h.name, "folderPath": h.folder_path,
                 "repoUrl": h.repo_url, "harvestedAt": h.harvested_at,
                 "path": str(h.path), "content": h.content}
                for h in rows
            ],
            sys.stdout, indent=2, ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0

    if not rows:
        print("no harvests yet")
        return 0
    for h in rows:
        preview = " ".join(h.content.split())[:80]
        print(f"{h.name or h.session_id}  [{h.repo_url or h.folder_path or '?'}]")
        print(f"  {h.folder_path or ''}  — {preview}")
    print(f"\n{len(rows)} harvest(s)")
    return 0
```

- [ ] **Step 4: Wire the dispatch**

In `ringleader/commands/__init__.py`, import `harvests` (as a command module — mind the name clash with the `ringleader.harvests` store module: import as e.g. `from . import harvests as harvests_cmd` or reference `harvests.cmd_harvests` from the commands package) and add top-level `"harvests": harvests_cmd.cmd_harvests` to `TABLE`. In `ringleader/cli.py`, add `"harvests"` to the `_FLAT` tuple so help lists it.

- [ ] **Step 5: Run tests + ruff**

Run: `uv run pytest tests/test_rl_harvests.py tests/test_rl_dispatch.py -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add ringleader/commands/harvests.py ringleader/commands/__init__.py ringleader/cli.py tests/test_rl_harvests.py tests/test_rl_dispatch.py
git commit -m "feat: add rl harvests to list the store"
```

---

### Task 5: `rl session complete` + docs

**Files:**
- Create: `ringleader/commands/complete.py`
- Modify: `ringleader/commands/archive.py` (extract `archive_thread`), `ringleader/commands/__init__.py`, `ringleader/cli.py`, `tests/test_rl_dispatch.py`, `README.md`, `AGENTS.md`
- Test: `tests/test_rl_complete.py`

**Interfaces:**
- Consumes: `harvest.cmd_harvest`'s flow, the extracted `archive.archive_thread`, `registry`.
- Produces: `commands.complete.cmd_complete(argv) -> int`; `_SESSION["complete"] = complete.cmd_complete` (de-reserved).

- [ ] **Step 1: Extract the archive helper**

In `ringleader/commands/archive.py`, factor the file-move + `registry.archive_session` core of `cmd_archive` into a reusable helper:

```python
def archive_thread(reg: dict, thread: dict, *, archive_root_path, summary_file=None) -> str:
    """Move the transcript (+ sidecar/summary) into archive_root and record the tombstone.
    Returns the destination path."""
    ...  # the body currently inside cmd_archive after the thread lookup
```

`cmd_archive` then looks up the thread and calls `archive_thread(...)`. Behavior unchanged — the existing `tests/test_rl_curation.py` archive tests must still pass.

- [ ] **Step 2: Write the failing tests**

`tests/test_rl_complete.py` — mock the model + render; assert harvest-then-archive, and no-archive on failure:

```python
import json

from ringleader.commands import complete as complete_mod


class FakeModel:
    name = "fake"

    def __init__(self, boom=False):
        self._boom = boom

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        from ringleader.models.base import ModelError
        if self._boom:
            raise ModelError("boom")
        return {"summary": "did it"}


def _seed(reg, tmp_path):
    f = tmp_path / "s1.jsonl"; f.write_text('{"type":"user","message":{"content":"hi"}}\n')
    reg.write_text(json.dumps({"version": 2, "threads": [
        {"sessionId": "s1", "file": str(f), "project": "/repo", "gitProjectLabel": "repo",
         "title": "t"}], "tombstones": []}))


def test_complete_harvests_then_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"; _seed(reg, tmp_path)
    monkeypatch.setattr(complete_mod.harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(complete_mod.harvest_mod.export, "render_conversation", lambda p: "hi")
    rc = complete_mod.cmd_complete(["s1", "--registry", str(reg)])
    assert rc == 0
    r = json.loads(reg.read_text())
    assert r["threads"] == [] or all(t["sessionId"] != "s1" for t in r["threads"])
    assert any(ts["sessionId"] == "s1" for ts in r["tombstones"])  # archived
    from ringleader import harvests
    assert len(harvests.load_harvests()) == 1


def test_complete_no_model_does_not_archive(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"; _seed(reg, tmp_path)
    monkeypatch.setattr(complete_mod.harvest_mod, "active_model", lambda cfg: None)
    rc = complete_mod.cmd_complete(["s1", "--registry", str(reg)])
    assert rc != 0 or "no model" in capsys.readouterr().out.lower()
    r = json.loads(reg.read_text())
    assert any(t["sessionId"] == "s1" for t in r["threads"])  # still live, not archived
```

Add to `tests/test_rl_dispatch.py`: move `session complete` out of `test_reserved_verbs_return_2` and into `test_new_paths_resolve` as `(["session", "complete"], commands.complete.cmd_complete)`.

- [ ] **Step 3: Implement `commands/complete.py`**

```python
from __future__ import annotations

import argparse

from .. import registry
from ..paths import archive_root
from . import archive as archive_cmd
from . import default_registry
from . import harvest as harvest_mod


def cmd_complete(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session complete")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    # Harvest first (writes the store + marks harvested + saves the registry).
    rc = harvest_mod.cmd_harvest([args.id, "--registry", args.registry])
    if rc != 0:
        return rc
    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None or t.get("decision") != "harvested":
        # harvest was a no-op (no model) — do not archive
        print("not completed: nothing was harvested (configure a model with `rl config`)")
        return 0
    dest = archive_cmd.archive_thread(reg, t, archive_root_path=str(archive_root()))
    registry.save_registry(args.registry, reg)
    print(f"completed {args.id} (archived -> {dest})")
    return 0
```

(Adjust to the exact `archive_thread` signature from Step 1.)

- [ ] **Step 4: Wire the dispatch (de-reserve `complete`)**

In `ringleader/commands/__init__.py`, replace `_SESSION["complete"]` value `_reserved("session complete")` with `complete.cmd_complete` (import the module). In `ringleader/cli.py`, remove `"session complete"` from `_RESERVED`.

- [ ] **Step 5: Run tests + ruff (full suite)**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean (existing archive + dispatch tests still green; `session complete` no longer exit-2).

- [ ] **Step 6: Docs**

- `README.md`: in the command table, replace the `rl session complete | *(coming soon)* — reserved.` row with a real description (harvest + archive); add `rl session harvest [--dry-run]` and a top-level `rl harvests [--json]` entry; add a short note that harvests are stored under `state_dir()/harvests/` and `rl harvests` is the inbox the skills integrate.
- `AGENTS.md`: add `rl session harvest`, `rl harvests`, and `rl session complete` to the commands block.

- [ ] **Step 7: Commit**

```bash
git add ringleader/commands/complete.py ringleader/commands/archive.py ringleader/commands/__init__.py ringleader/cli.py tests/test_rl_complete.py tests/test_rl_dispatch.py README.md AGENTS.md
git commit -m "feat: add rl session complete (harvest + archive)"
```

---

## Self-Review

**Spec coverage:**
- Condensed input (tool output stripped) → Task 1 (`render_conversation` already strips it; Codex support added). ✓
- `rl session harvest` (distill → store → mark harvested; degradation; dry-run) → Task 3. ✓
- Harvest store with folderPath/repoUrl/content + metadata → Task 2 + Task 3. ✓
- `rl harvests` (folder path, repo url, content; `--json`) → Task 4. ✓
- `rl session complete` = harvest + archive; no archive if harvest can't run → Task 5. ✓
- Reuse C1 Model seam; backend-agnostic; no secrets/deps → Tasks 3/5 mirror `enrich`. ✓
- Reserved `complete` implemented; dispatch/cli/docs updated → Task 5. ✓
- Tests never call real model / scan real data → all tasks mock the model + isolate `XDG_DATA_HOME`/`--registry`. ✓

**Placeholder scan:** No TBD/TODO. Two verify-and-adapt notes (memory.parse_frontmatter suitability; exact `archive_thread` signature) are concrete extraction/verification steps, not placeholders. `--full` intentionally dropped (documented in Global Constraints).

**Type consistency:** `render_conversation(path) -> str`; `write_harvest(*, …) -> Path`; `load_harvests() -> list[Harvest]`; `active_model(cfg) -> Model | None`; `complete_json(prompt, schema) -> dict`; `mark_harvested(reg, id, paths)`; harvest metadata keys (`folderPath`/`repoUrl`/`sessionId`/`name`/`harvestedAt`) consistent between `write_harvest` and `load_harvests` and the `--json` output.

## Out of scope

- Integrating harvests into repo docs (skills layer / user).
- The TUI (Spec 3); any `harvest.command`/`harvest.skill` hook; renaming `archive`.
- A `--full` (raw, with tool output) harvest input.
