# Codex Session Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Codex as a second session `Source` reading `~/.codex/sessions/`, proving the `sources/` seam generalizes — Codex sessions flow through the same scan → registry → `overview`/`triage`/`prune` pipeline as Claude, with source visible/filterable and the Claude-specific leaks fixed.

**Architecture:** A new `CodexSource` implements the existing `Source` protocol; the scanner auto-detects installed sources; the registry persists `source` and keys identity by `(source, session_id)`; a per-source capability set gates harness-specific operations (name writeback); and the archive/prune/name commands are generalized off Claude-specific assumptions.

**Tech Stack:** Python ≥3.13, stdlib only (JSONL parsing), `pytest` (via `uv run pytest`), `ruff`.

## Global Constraints

- **Validation spec, not parity.** Prove the seam; do not build Codex-specific mechanisms (naming writeback, memory) — those are absent/deferred.
- **No new runtime dependencies.** `pyproject.toml` `dependencies` stays empty; Codex JSONL is parsed with stdlib `json`.
- **Auto-detect sources.** A source is active iff its root exists on disk. No config.
- **Registry identity carries `source`.** Threads persist a `source` field; reconcile keys by `(source, session_id)`, defaulting a missing/legacy `source` to `"claude-code"` so existing Claude threads still match.
- **Capabilities:** `ClaudeCodeSource.capabilities == frozenset({"applies_names"})`; `CodexSource.capabilities == frozenset()`.
- **Source is visible + filterable:** a source badge in the per-session `overview`/`triage` output; a repeatable `--source <name>` filter.
- **Accepted behavior change:** `rl session archive` destination folders derive from the project label, not the transcript's parent-dir name.
- **Prune tininess keys on `message_count`**, not bytes (Codex rollout files bundle tool events).
- **Keep source registration general** — do not hardcode "sources are session sources" (a memory source lands in Spec B).
- Tests pass with `uv run pytest -q`; `uv run ruff check` and `uv run ruff format --check` clean.

## Codex format (reference)

- `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<session_id>.jsonl`.
- Line 1: `{"type":"session_meta","payload":{"session_id":..,"cwd":..,"timestamp":..,"model_provider":..}}`.
- User messages: lines with `type=="response_item"`, `payload.type=="message"`, `payload.role=="user"`; text in `payload.content[].text`.
- The **first user message is an injected `AGENTS.md` preamble** — text begins with `# AGENTS.md` and/or contains `<INSTRUCTIONS>`; it must be excluded from `message_count`/`last_prompt`.
- No custom-title / summary / ai-title.

## File Structure

- `ringleader/sources/codex.py` — **create.** `CodexSource` (discover/parse over `~/.codex/sessions`).
- `ringleader/sources/base.py` — **modify.** Add `capabilities` to the `Source` protocol.
- `ringleader/sources/claude_code.py` — **modify.** Declare `capabilities` + a `root` property.
- `ringleader/scanner.py` — **modify.** `active_sources()` (auto-detect) + `source_by_name()`; register `CodexSource`.
- `ringleader/commands/sync.py` — **modify.** `cmd_scan` scans `active_sources()`.
- `ringleader/registry.py` — **modify.** Persist `source`; reconcile keyed by `(source, session_id)`.
- `ringleader/overview.py` — **modify.** Source badge in `render` + `render_triage`.
- `ringleader/commands/overview.py`, `ringleader/commands/triage.py` — **modify.** `--source` filter.
- `ringleader/commands/archive.py` — **modify.** Project-label archive destination.
- `ringleader/commands/name.py` — **modify.** Gate on the `applies_names` capability.
- `ringleader/commands/prune.py` — **modify.** Tininess by `message_count`.
- `tests/test_sources_codex.py`, `tests/test_scanner.py`, `tests/test_registry.py`, `tests/test_rl_overview.py`, `tests/test_rl_triage.py`, `tests/test_rl_curation.py`, `tests/test_rl_name.py`, `tests/test_rl_prune.py` — **create/modify** as noted per task.

## Reference: how the pieces connect

- `scanner.scan(sources=None)` iterates `sources` (default `SOURCES`), calls `src.discover()` then `src.parse(ref)`, and fills `git_*` centrally from `cwd` via `resolve_git_project` — **so a source only needs to supply `cwd`, never resolve git itself.**
- `NormalizedSession` (`sources/base.py`) already has a `source: str` field and defaulted extras; `custom_title`/`summary`/`ai_title` default to `None`.
- `registry.reconcile(reg, scanned, now)` builds `by_id` from existing threads, updates matches via `_METADATA_FIELDS`/`_GIT_PROJECT_FIELDS`, and `_new_thread(md)` for new ones. Thread records currently have **no** `source` key.
- The `rl session <sub>` CLI dispatches to `commands/*.py`; each `cmd_*` parses its own argv.

---

### Task 1: CodexSource — discover + parse

**Files:**
- Create: `ringleader/sources/codex.py`
- Test: `tests/test_sources_codex.py`

**Interfaces:**
- Produces:
  - `ringleader.sources.codex.default_codex_dir() -> Path` (env override `CODEX_SESSIONS_DIR`, else `~/.codex/sessions`).
  - `CodexSource` dataclass: fields `sessions_dir: Path` (default `default_codex_dir()`), `tail_turns: int = 30`, `name: str = "codex"`, `capabilities: frozenset[str] = frozenset()`; property `root -> Path` returning `self.sessions_dir`; methods `discover() -> Iterator[RawRef]` and `parse(ref) -> NormalizedSession` (source `"codex"`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources_codex.py`:

```python
import json
from pathlib import Path

from ringleader.sources.base import RawRef, Source
from ringleader.sources.codex import CodexSource


def _rollout(dir_: Path, sid: str, cwd: str, users: list[str], *, preamble: bool = True) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session_meta", "payload": {"session_id": sid, "cwd": cwd,
                                             "timestamp": "2026-07-28T17:19:45.210Z",
                                             "model_provider": "openai"}},
    ]
    msgs = (["# AGENTS.md instructions for %s\n<INSTRUCTIONS>...</INSTRUCTIONS>" % cwd] if preamble else []) + users
    for i, text in enumerate(msgs):
        role = "user"
        lines.append({"type": "response_item",
                      "payload": {"type": "message", "role": role,
                                  "content": [{"type": "input_text", "text": text}]}})
    p = dir_ / f"rollout-2026-07-28T19-19-45-{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_source_satisfies_protocol(tmp_path):
    src = CodexSource(sessions_dir=tmp_path)
    assert isinstance(src, Source)
    assert src.name == "codex"
    assert src.capabilities == frozenset()


def test_discover_finds_rollouts(tmp_path):
    _rollout(tmp_path / "2026" / "07" / "28", "11111111-0000-0000-0000-000000000001",
             "/Users/me/proj", ["hi"])
    src = CodexSource(sessions_dir=tmp_path)
    refs = list(src.discover())
    assert len(refs) == 1
    r = refs[0]
    assert isinstance(r, RawRef) and r.source == "codex" and r.file.endswith(".jsonl")


def test_parse_extracts_fields_and_skips_preamble(tmp_path):
    d = tmp_path / "2026" / "07" / "28"
    _rollout(d, "22222222-0000-0000-0000-000000000002", "/Users/me/proj",
             ["first real prompt", "the last prompt"])
    src = CodexSource(sessions_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.source == "codex"
    assert ns.session_id == "22222222-0000-0000-0000-000000000002"
    assert ns.cwd == "/Users/me/proj"
    assert ns.last_prompt == "the last prompt"
    assert ns.message_count == 2  # preamble excluded
    assert ns.custom_title is None and ns.summary is None and ns.ai_title is None


def test_discover_missing_dir_yields_nothing(tmp_path):
    assert list(CodexSource(sessions_dir=tmp_path / "nope").discover()) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_sources_codex.py -q`
Expected: FAIL (`ModuleNotFoundError: ringleader.sources.codex`).

- [ ] **Step 3: Implement `CodexSource`**

Create `ringleader/sources/codex.py`:

```python
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .base import NormalizedSession, RawRef


def default_codex_dir() -> Path:
    env = os.environ.get("CODEX_SESSIONS_DIR")
    return Path(env) if env else Path.home() / ".codex" / "sessions"


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()


def _is_preamble(text: str) -> bool:
    t = text.lstrip()
    return t.startswith("# AGENTS.md") or "<INSTRUCTIONS>" in text


def _user_texts(path: Path) -> tuple[str | None, str | None, int]:
    """Return (session_id, cwd, ...) is handled by caller; here return the real
    (non-preamble) user prompts. Returns (first, last, count)."""
    prompts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "response_item":
            continue
        p = o.get("payload", {})
        if p.get("type") != "message" or p.get("role") != "user":
            continue
        text = " ".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict)).strip()
        if not text or _is_preamble(text):
            continue
        prompts.append(text)
    first = prompts[0] if prompts else None
    last = prompts[-1] if prompts else None
    return first, last, len(prompts)


def _meta(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            o = json.loads(line)
            if o.get("type") == "session_meta":
                return o.get("payload", {})
            return {}
    return {}


@dataclass
class CodexSource:
    sessions_dir: Path = field(default_factory=default_codex_dir)
    tail_turns: int = 30
    name: str = "codex"
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self.sessions_dir = Path(self.sessions_dir)

    @property
    def root(self) -> Path:
        return self.sessions_dir

    def discover(self) -> Iterator[RawRef]:
        if not self.sessions_dir.exists():
            return
        for p in sorted(self.sessions_dir.glob("**/*.jsonl")):
            try:
                st = p.stat()
            except OSError as e:
                print(f"skipped {p}: {e}", file=sys.stderr)
                continue
            yield RawRef(source=self.name, file=str(p), mtime=_iso_mtime(p), size_bytes=st.st_size)

    def parse(self, ref: RawRef) -> NormalizedSession:
        path = Path(ref.file)
        meta = _meta(path)
        _first, last, count = _user_texts(path)
        tail = last or ""
        return NormalizedSession(
            source=self.name,
            session_id=meta.get("session_id") or path.stem,
            cwd=meta.get("cwd"),
            mtime=ref.mtime,
            size_bytes=ref.size_bytes,
            custom_title=None,
            summary=None,
            last_prompt=last,
            file=ref.file,
            ai_title=None,
            message_count=count,
            tail_text=tail[:6000],
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_sources_codex.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ringleader/sources/codex.py tests/test_sources_codex.py
git commit -m "feat: add a Codex session source"
```

---

### Task 2: Source capabilities + multi-source auto-detect

**Files:**
- Modify: `ringleader/sources/base.py`, `ringleader/sources/claude_code.py`, `ringleader/scanner.py`, `ringleader/commands/sync.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `CodexSource` (Task 1).
- Produces:
  - `Source` protocol gains `capabilities: frozenset[str]`.
  - `ClaudeCodeSource.capabilities == frozenset({"applies_names"})` and a `root` property returning `self.projects_dir`.
  - `scanner.SOURCES: list[Source]` (instances of `ClaudeCodeSource()` and `CodexSource()`).
  - `scanner.active_sources() -> list[Source]` — every source in `SOURCES` whose `root` exists.
  - `scanner.source_by_name(name: str) -> Source | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scanner.py`:

```python
def test_active_sources_autodetects_by_root(tmp_path, monkeypatch):
    claude = tmp_path / "claude"; codex = tmp_path / "codex"
    claude.mkdir()  # only claude root exists
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(claude))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(codex))
    from ringleader import scanner
    names = {s.name for s in scanner.active_sources()}
    assert names == {"claude-code"}
    codex.mkdir()
    assert {s.name for s in scanner.active_sources()} == {"claude-code", "codex"}


def test_capabilities_declared():
    from ringleader.sources.claude_code import ClaudeCodeSource
    from ringleader.sources.codex import CodexSource
    assert "applies_names" in ClaudeCodeSource().capabilities
    assert CodexSource().capabilities == frozenset()


def test_source_by_name():
    from ringleader import scanner
    assert scanner.source_by_name("codex").name == "codex"
    assert scanner.source_by_name("nope") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scanner.py -q`
Expected: FAIL (`active_sources`/`source_by_name` undefined; `capabilities` missing).

- [ ] **Step 3: Add `capabilities` to the protocol and Claude source**

In `ringleader/sources/base.py`, add to the `Source` protocol body (after `name: str`):

```python
    capabilities: frozenset[str]
```

In `ringleader/sources/claude_code.py`, add to the `ClaudeCodeSource` dataclass fields and a `root` property:

```python
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({"applies_names"}))

    @property
    def root(self) -> Path:
        return self.projects_dir
```

(`field` is already imported in that file.)

- [ ] **Step 4: Add the source registry to scanner.py**

Replace `SOURCES = [ClaudeCodeSource()]` and add helpers:

```python
from .sources.codex import CodexSource

SOURCES = [ClaudeCodeSource(), CodexSource()]


def active_sources() -> list:
    return [s for s in SOURCES if s.root.exists()]


def source_by_name(name: str):
    return next((s for s in SOURCES if s.name == name), None)
```

Keep `scan(sources=None, ...)` defaulting to `SOURCES` (unchanged) so existing tests that call `scan()` still see both registered sources.

- [ ] **Step 5: Point `cmd_scan` at active sources**

In `ringleader/commands/sync.py` `cmd_scan`, change the `scanner.scan(...)` call to pass detected sources. Current call constructs `[ClaudeCodeSource(tail_turns=args.tail)]`; replace with `scanner.active_sources()` (import `scanner`). Note `active_sources()` uses default `tail_turns`; that is acceptable (30 default matches the old `--tail` default). If `--tail` must still apply, construct per-source with the arg — but the default path is fine for this task.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_scanner.py tests/test_sources_codex.py -q && uv run ruff check`
Expected: PASS / clean.

- [ ] **Step 7: Commit**

```bash
git add ringleader/sources/base.py ringleader/sources/claude_code.py ringleader/scanner.py ringleader/commands/sync.py tests/test_scanner.py
git commit -m "feat: auto-detect installed sources and declare source capabilities"
```

---

### Task 3: Registry — persist source + composite identity

**Files:**
- Modify: `ringleader/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `NormalizedSession.source` (already present).
- Produces: thread records carry a `"source"` key; `reconcile` matches by `(source, session_id)` with a `"claude-code"` default for legacy sourceless threads.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_registry.py` (reuse the existing `NormalizedSession` fixture style in that file; construct sessions with explicit `source`):

```python
def _ns(**kw):
    from ringleader.sources.base import NormalizedSession
    base = dict(source="claude-code", session_id="s1", cwd="/p", mtime="t", size_bytes=10,
                custom_title=None, summary=None, last_prompt=None, file="/p/s1.jsonl")
    base.update(kw)
    return NormalizedSession(**base)


def test_reconcile_persists_source():
    from ringleader import registry
    reg = registry.empty_registry()
    reg = registry.reconcile(reg, [_ns(source="codex", session_id="c1")], "now")
    assert reg["threads"][0]["source"] == "codex"


def test_reconcile_backfills_source_on_legacy_thread():
    from ringleader import registry
    reg = registry.empty_registry()
    reg["threads"].append({"sessionId": "s1", "file": "/p/s1.jsonl"})  # legacy: no source
    reg = registry.reconcile(reg, [_ns(source="claude-code", session_id="s1")], "now")
    # matched (not duplicated / tombstoned) and stamped with source
    assert len(reg["threads"]) == 1
    assert reg["threads"][0]["source"] == "claude-code"
    assert reg["tombstones"] == []


def test_reconcile_distinguishes_same_id_across_sources():
    from ringleader import registry
    reg = registry.empty_registry()
    reg = registry.reconcile(reg, [_ns(source="claude-code", session_id="x"),
                                    _ns(source="codex", session_id="x")], "now")
    assert len(reg["threads"]) == 2
    assert {t["source"] for t in reg["threads"]} == {"claude-code", "codex"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_registry.py -q -k "source or backfill or distinguishes"`
Expected: FAIL (`KeyError: 'source'` / duplicate/tombstoned legacy thread).

- [ ] **Step 3: Persist source**

In `ringleader/registry.py`:
- Add `"source": "source"` to the `_METADATA_FIELDS` dict (so existing threads are stamped on every reconcile).
- Add `"source": md["source"],` to the dict returned by `_new_thread`.

- [ ] **Step 4: Key reconcile by (source, session_id)**

In `reconcile`, replace the identity handling. Define a key helper and use it for `by_id`, `seen`, and tombstoning:

```python
def _key(rec: dict) -> tuple:
    return (rec.get("source") or "claude-code", rec["sessionId"])
```

- `by_id = {_key(t): t for t in reg["threads"]}`
- per scanned `md`: `k = (md["source"], md["session_id"])`; `seen.add(k)`; `existing = by_id.get(k)`; keep the existing update / `_new_thread(md)` branches but keyed on `k`.
- tombstone loop: `if _key(t) not in seen and t["sessionId"] not in tombstoned:` (tombstones remain keyed by `sessionId` — a session that vanished is gone regardless of source; keep the existing `tombstoned` set of session ids).

Legacy threads with no `source` default to `"claude-code"` via `_key`, matching a Claude scan's `("claude-code", sid)` — so they update in place rather than duplicating.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_registry.py -q`
Expected: PASS (new + existing registry tests).

- [ ] **Step 6: Commit**

```bash
git add ringleader/registry.py tests/test_registry.py
git commit -m "feat: persist source and key registry identity by (source, session_id)"
```

---

### Task 4: Views — source badge + `--source` filter

**Files:**
- Modify: `ringleader/overview.py`, `ringleader/commands/overview.py`, `ringleader/commands/triage.py`
- Test: `tests/test_rl_overview.py`, `tests/test_rl_triage.py`

**Interfaces:**
- Consumes: thread `"source"` key (Task 3).
- Produces: a source badge column in `overview.render` (the `-s` sessions list) and `overview.render_triage`; a repeatable `--source <name>` filter on `cmd_overview` and `cmd_triage` that filters threads before rendering. Badge display shortens `"claude-code"` → `claude`.

- [ ] **Step 1: Write the failing tests**

Add subprocess-style tests (matching the existing `_run` helper in each file). Seed a tmp registry with two threads of different sources (write `registry.json` directly with `source` set), then:

```python
def test_sessions_list_shows_source_badge(...):
    # rl overview -s --registry <tmp> --no-scan  -> output contains "codex" and "claude"
    ...

def test_source_filter_limits_to_codex(...):
    # rl overview -s --source codex --registry <tmp> --no-scan  -> only codex thread shown
    ...

def test_triage_source_filter(...):
    # rl triage --source codex --registry <tmp> --no-scan  -> only codex thread
    ...
```

Follow each test file's existing registry-seeding + `_run` conventions (see `tests/test_rl_overview.py` for how a registry is written and `--no-scan` is used to avoid a live scan). Each thread dict needs at least `sessionId`, `file`, `project`, `status`, `mtime`, and now `source`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_rl_overview.py tests/test_rl_triage.py -q -k source`
Expected: FAIL (no badge; `--source` unrecognized).

- [ ] **Step 3: Add the badge to rendering**

In `ringleader/overview.py`, add a helper and use it in `render` (the per-session `-s` list) and `render_triage` when emitting each session row:

```python
def source_badge(thread: dict) -> str:
    s = thread.get("source") or "claude-code"
    return "claude" if s == "claude-code" else s
```

Insert the badge as a short leading column on each session line in `render` and `render_triage` (match the existing column layout in those functions — read them first; keep alignment consistent). Do not alter the rollup (`render_rollup`) — source is a per-session attribute.

- [ ] **Step 4: Add the `--source` filter to the commands**

In `ringleader/commands/overview.py` (`cmd_overview`) and `ringleader/commands/triage.py` (`cmd_triage`), add:

```python
ap.add_argument("--source", action="append", default=None,
                help="filter to sessions from this source (repeatable)")
```

After loading the registry and before rendering, when `args.source` is set, filter the thread list the view renders to those whose `(t.get("source") or "claude-code")` is in `set(args.source)`. Apply it to the sessions-list / triage paths (not the memory paths).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_rl_overview.py tests/test_rl_triage.py -q && uv run ruff check`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add ringleader/overview.py ringleader/commands/overview.py ringleader/commands/triage.py tests/test_rl_overview.py tests/test_rl_triage.py
git commit -m "feat: show a source badge and add a --source filter"
```

---

### Task 5: Seam-leak fixes — archive dest, name capability, prune tininess, audit

**Files:**
- Modify: `ringleader/commands/archive.py`, `ringleader/commands/name.py`, `ringleader/commands/prune.py`
- Test: `tests/test_rl_curation.py`, `tests/test_rl_name.py`, `tests/test_rl_prune.py`

**Interfaces:**
- Consumes: thread `"source"` (Task 3), `scanner.source_by_name` + capabilities (Task 2).

- [ ] **Step 1: Write the failing tests**

- In `tests/test_rl_curation.py`: seed a Codex thread (`source: "codex"`, a real tmp `.jsonl` file, `gitProjectLabel: "proj"`), run `session archive` on it, assert the archived file landed under a `proj/` folder in the archive root (not under a day-number), and the tombstone was recorded.
- In `tests/test_rl_name.py`: seed one Claude thread and one Codex thread both eligible for naming; run `session name`; assert the Claude one is named (title record appended, `appliedName` set) and the Codex one is skipped, with the output reporting a skipped-for-capability count. Codex's `suggestedName` remains visible in `overview`.
- In `tests/test_rl_prune.py`: seed a Codex thread with `messageCount: 2` but `sizeBytes` above the size ceiling (e.g. 200 KB); assert it is prune-eligible (tininess driven by messages, not bytes) given it is also stale.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_rl_curation.py tests/test_rl_name.py tests/test_rl_prune.py -q -k "codex or source or capability"`
Expected: FAIL.

- [ ] **Step 3: Source-neutral archive destination**

In `ringleader/commands/archive.py`, replace `dest_dir = Path(args.archive_root) / src.parent.name` with a project-label-derived folder:

```python
slug = t.get("gitProjectLabel") or (Path(t["project"]).name if t.get("project") else None) or t.get("source") or "unknown"
dest_dir = Path(args.archive_root) / slug
```

(`t` is the registry thread already looked up in `cmd_archive`.) The rest of the archive flow (moving the transcript + sidecar, tombstone) is unchanged.

- [ ] **Step 4: Gate naming on the `applies_names` capability**

In `ringleader/commands/name.py`, after `selected` is computed, drop threads whose source lacks the capability and count them:

```python
from .. import scanner

def _can_name(t: dict) -> bool:
    src = scanner.source_by_name(t.get("source") or "claude-code")
    return bool(src and "applies_names" in src.capabilities)

cap_skipped = [s for s in selected if not _can_name(s[0])]
selected = [s for s in selected if _can_name(s[0])]
```

Include `cap_skipped` in the final summary line, e.g. `... skipped {skipped} ({len(cap_skipped)} no-name-support)`. Names still appear in `overview` because they come from classification, not from this command.

- [ ] **Step 5: Prune tininess by message count**

In `ringleader/commands/prune.py`, make the "tiny" test driven by `messageCount` so Codex sessions with event-heavy byte sizes are still eligible. Read the current eligibility helper (the `max_size_bytes`/`max_messages` logic around `cmd_prune`): change it so a session is "tiny" when `messageCount <= max_messages` (source-neutral), rather than requiring the byte size to also be under `max_size_bytes`. Keep the byte size in the displayed row. Keep the staleness (age) test unchanged.

- [ ] **Step 6: Audit pass for remaining Claude assumptions**

Grep the command layer + `registry.py`/`overview.py` for Claude-specific assumptions and resolve each (generalize, or add a one-line comment documenting it as Claude-only):

```bash
grep -rnE "\.claude|/projects/|subagents|custom-title|projects_dir" ringleader/commands ringleader/registry.py ringleader/overview.py
```

Record findings + resolutions in the task report.

- [ ] **Step 7: Run tests + ruff**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean.

- [ ] **Step 8: Commit**

```bash
git add ringleader/commands/archive.py ringleader/commands/name.py ringleader/commands/prune.py tests/test_rl_curation.py tests/test_rl_name.py tests/test_rl_prune.py
git commit -m "fix: generalize archive, name, and prune off Claude-specific assumptions"
```

---

## Self-Review

**Spec coverage:**
- CodexSource (discover/parse, preamble skip, no title/summary) → Task 1. ✓
- Auto-detect sources → Task 2 (`active_sources`). ✓
- Capabilities (`applies_names`) → Task 2 declares, Task 5 gates `name`. ✓
- `(source, session_id)` identity + persist source + legacy backfill → Task 3. ✓
- Source badge + `--source` filter → Task 4. ✓
- Archive dest source-neutral → Task 5 step 3. ✓
- Prune by message_count → Task 5 step 5. ✓
- Audit pass → Task 5 step 6. ✓
- git resolved centrally by scanner (source supplies only cwd) → reflected in Task 1 (no gitinfo in CodexSource). ✓
- Out of scope (memory, config, name-writeback) → not implemented; source registration kept general (list of sources, not hardcoded). ✓

**Placeholder scan:** Task 4's view tests are described against each file's existing `_run`/registry-seeding conventions rather than transcribed verbatim (those helpers differ per file and must be matched in place) — the assertions and commands are concrete. All code steps carry real code.

**Type consistency:** `capabilities: frozenset[str]` on both sources and the protocol; `source_by_name(name) -> Source | None`; `active_sources() -> list`; thread `"source"` key read consistently as `t.get("source") or "claude-code"` in reconcile keying, badge, filter, and capability lookup; `NormalizedSession(source="codex", …)` matches the dataclass field order/defaults.

## Out of scope

- Codex **memory** (`memories_1.sqlite`) — Spec B.
- Any **config** — sources auto-detected only.
- **Name writeback** for Codex — capability simply absent.
- LLM enrichment / TUI — later specs.
