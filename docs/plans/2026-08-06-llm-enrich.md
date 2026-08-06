# LLM Foundation + Enrich Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ringleader a tool-native LLM capability — a `Model` seam with agent-CLI and API backends, `rl config`, and `rl session enrich` that classifies/names/summarizes dormant sessions by calling a model directly.

**Architecture:** A `Model` seam (parallel to `sources/`) abstracts `complete_json(prompt, schema) -> dict`. Three hand-rolled backends: `ClaudeCliModel`/`CodexCliModel` (one-shot CLI invocation reusing existing auth) and `ApiModel` (the `llm` library, API key). `rl config` picks the backend (non-secret). `rl session enrich` scans for pending sessions, calls the configured model per session, and writes the classification back via `registry.apply_classification`.

**Tech Stack:** Python ≥3.13; stdlib `subprocess`/`json`/`shutil` for the agent backends; `llm` for the API backend (optional extra `ringleader[api]`); `pytest`, `ruff`.

## Global Constraints

- **The Model seam mirrors the Source seam** (`ringleader/sources/base.py` + `scanner.py`): a `Model` protocol with `name`, `available()`, `complete_json`; a `SOURCES`-style registry; an `active_model()` resolver analogous to `active_sources()`.
- **ringleader stores no secrets.** Agent backends reuse Claude Code/Codex auth; the API backend delegates keys to `llm`. `rl config` holds only non-secret selections.
- **No new *required* runtime dependency.** `pyproject.toml` `dependencies` stays `[]`; `llm` goes under `[project.optional-dependencies]` as `api`. The API backend imports `llm` lazily and degrades if absent.
- **Zero-config default:** `active_model()` uses the configured backend if set+available; else auto-picks the first available agent backend in order (claude, then codex); else `None`.
- **Graceful degradation:** no available model → `enrich` prints the guidance message and is a no-op (exit 0). The `session-sync` skill is NOT deleted; enrich fills the same `status`/`whatItIs`/`suggestedName` fields.
- **External CLI flags must be verified against the installed tools.** `claude`/`codex` flag names evolve; the implementer verifies the exact invocation against `claude --help` / `codex exec --help` on this machine and records the working command. **Tests never call a real model or spawn an agent** — they mock `subprocess.run` / the `llm` call.
- **Config is JSON** at `state_dir() / "config.json"` (stdlib read+write; chosen over TOML because stdlib has no TOML *writer* and `rl config set` must write).
- Tests pass with `uv run pytest -q`; `uv run ruff check` and `uv run ruff format --check` clean.

## Reference: verified current interfaces

- `registry.apply_classification(reg, session_id, *, status, what_it_is, suggested_name, confidence=None) -> dict` (registry.py:170).
- `registry.sessions_needing_classification(reg) -> list[dict]` (registry.py:166); `registry.reconcile(reg, scanned, now_iso)`; `registry.load_registry(path)` / `save_registry(path, reg)`.
- `scanner.scan(sources=None, git_resolver=None) -> list[NormalizedSession]` (returns `NormalizedSession` with transient `.tail_text`); `scanner.active_sources() -> list`.
- `paths.state_dir() -> Path` (paths.py:7); `paths.default_registry()`.
- `sources/base.py` pattern to mirror: a `@runtime_checkable` `Protocol` with attributes + methods; `NormalizedSession` has `session_id`, `tail_text`, `cwd`, `git_branch`, `message_count`, `last_prompt`.
- Reserved stubs to replace in `commands/__init__.py` TABLE: `_SESSION["enrich"]` (line ~41) and top-level `"config"` (line ~80), both currently `_reserved(...)`.
- `pyproject.toml`: `dependencies = []` (line 11); `[project.scripts] rl = "ringleader.cli:main"`.

## File Structure

- `ringleader/models/__init__.py` — **create.** `active_model()`, `MODELS` registry, re-exports.
- `ringleader/models/base.py` — **create.** `Model` protocol + `ModelError`.
- `ringleader/models/claude_cli.py` — **create.** `ClaudeCliModel`.
- `ringleader/models/codex_cli.py` — **create.** `CodexCliModel`.
- `ringleader/models/api.py` — **create.** `ApiModel` (lazy `llm`).
- `ringleader/config.py` — **create.** `Config` dataclass, `load_config`/`save_config`.
- `ringleader/paths.py` — **modify.** add `config_path()`.
- `ringleader/commands/config.py` — **create.** `cmd_config` (`show`/`set`).
- `ringleader/commands/enrich.py` — **create.** `cmd_enrich`.
- `ringleader/commands/__init__.py` — **modify.** wire `config` + `session enrich`.
- `pyproject.toml` — **modify.** add `[project.optional-dependencies] api = ["llm"]`.
- `README.md`, `AGENTS.md` — **modify.** document `rl config`, `rl session enrich`, backends.
- Tests: `tests/test_models.py`, `tests/test_config.py`, `tests/test_rl_enrich.py`, `tests/test_rl_config.py` — **create.**

---

### Task 1: Model seam + backends

**Files:**
- Create: `ringleader/models/base.py`, `ringleader/models/claude_cli.py`, `ringleader/models/codex_cli.py`, `ringleader/models/api.py`, `ringleader/models/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `models.base.Model` (`@runtime_checkable Protocol`): `name: str`, `available(self) -> bool`, `complete_json(self, prompt: str, schema: dict) -> dict`.
  - `models.base.ModelError(Exception)`.
  - `ClaudeCliModel(name="claude", model: str | None = None)`, `CodexCliModel(name="codex", model: str | None = None)`, `ApiModel(name="api", provider: str = "anthropic", model: str | None = None)`.
  - `models.active_model(cfg) -> Model | None` and `models.MODELS` (the agent-backend auto-pick order: `[ClaudeCliModel, CodexCliModel]`).

- [ ] **Step 1: Write failing tests**

`tests/test_models.py`:

```python
import json
import subprocess
import types

import pytest

from ringleader import models
from ringleader.models.base import Model, ModelError
from ringleader.models.claude_cli import ClaudeCliModel
from ringleader.models.codex_cli import CodexCliModel

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


def test_backends_satisfy_protocol():
    assert isinstance(ClaudeCliModel(), Model)
    assert isinstance(CodexCliModel(), Model)


def test_claude_available_follows_which(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: "/bin/claude")
    assert ClaudeCliModel().available() is True
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    assert ClaudeCliModel().available() is False


def test_claude_complete_json_builds_command_and_parses_result(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        # claude --output-format json wraps the answer in a {"result": "..."} envelope
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"result": json.dumps({"x": "hi"})}), stderr="")

    monkeypatch.setattr("ringleader.models.claude_cli.subprocess.run", fake_run)
    out = ClaudeCliModel().complete_json("classify this", SCHEMA)
    assert out == {"x": "hi"}
    assert "claude" in seen["cmd"][0] or seen["cmd"][0] == "claude"
    assert "-p" in seen["cmd"]


def test_claude_raises_modelerror_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.claude_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(ModelError):
        ClaudeCliModel().complete_json("p", SCHEMA)


def test_codex_complete_json_parses_schema_output(monkeypatch):
    monkeypatch.setattr(
        "ringleader.models.codex_cli.subprocess.run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=json.dumps({"x": "yo"}), stderr=""),
    )
    assert CodexCliModel().complete_json("p", SCHEMA) == {"x": "yo"}


def test_active_model_autopicks_first_available_agent(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    monkeypatch.setattr("ringleader.models.codex_cli.shutil.which", lambda n: "/bin/codex")
    from ringleader.config import Config

    m = models.active_model(Config())  # no explicit backend
    assert m is not None and m.name == "codex"


def test_active_model_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr("ringleader.models.claude_cli.shutil.which", lambda n: None)
    monkeypatch.setattr("ringleader.models.codex_cli.shutil.which", lambda n: None)
    from ringleader.config import Config

    assert models.active_model(Config()) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_models.py -q`
Expected: FAIL (`ringleader.models` missing). (`ringleader.config.Config` is Task 2 — this task may stub a minimal `Config` import; if the test import fails on config, create `ringleader/config.py` with just the `Config` dataclass now and flesh it out in Task 2.)

- [ ] **Step 3: `models/base.py`**

```python
from __future__ import annotations

from typing import Protocol, runtime_checkable


class ModelError(Exception):
    pass


@runtime_checkable
class Model(Protocol):
    name: str

    def available(self) -> bool: ...
    def complete_json(self, prompt: str, schema: dict) -> dict: ...
```

- [ ] **Step 4: `models/claude_cli.py`**

Verify the real flags first: run `claude --help` and confirm `-p/--print`, `--output-format json`, the schema flag (`--json-schema` or current equivalent), and how to disable tool use for a pure answer (e.g. `--permission-mode`/no allowed tools). Record the working command in the report. Then:

```python
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .base import ModelError


@dataclass
class ClaudeCliModel:
    name: str = "claude"
    model: str | None = None

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def complete_json(self, prompt: str, schema: dict) -> dict:
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "schema.json"
            sf.write_text(json.dumps(schema), encoding="utf-8")
            cmd = ["claude", "-p", "--output-format", "json", "--json-schema", str(sf)]
            if self.model:
                cmd += ["--model", self.model]
            # constrain to answer-only per the verified --help (no tool use)
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True)
            except OSError as e:  # CLI vanished mid-run
                raise ModelError(f"claude invocation failed: {e}") from e
        if proc.returncode != 0:
            raise ModelError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            envelope = json.loads(proc.stdout)
            result = envelope["result"] if isinstance(envelope, dict) and "result" in envelope else envelope
            return result if isinstance(result, dict) else json.loads(result)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ModelError(f"claude returned unparseable output: {e}") from e
```

- [ ] **Step 5: `models/codex_cli.py`**

Verify `codex exec --help` for the schema flag (`--output-schema`) and no-tool/sandbox flags; record the working command. Then implement `CodexCliModel` mirroring the Claude backend: build `["codex", "exec", "--output-schema", <schema file>, prompt]` (plus `--model` when set), run, raise `ModelError` on non-zero/unparseable, and parse the schema-conformant JSON from stdout (Codex emits the object directly — no `result` envelope; confirm during verification and adapt the parse).

- [ ] **Step 6: `models/api.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from .base import ModelError


@dataclass
class ApiModel:
    name: str = "api"
    provider: str = "anthropic"
    model: str | None = None

    def _llm(self):
        try:
            import llm
        except ImportError as e:
            raise ModelError("the api backend needs `pip install ringleader[api]`") from e
        return llm

    def available(self) -> bool:
        try:
            llm = self._llm()
        except ModelError:
            return False
        try:
            llm.get_model(self.model or self.provider)  # resolves + checks a key is set
            return True
        except Exception:
            return False

    def complete_json(self, prompt: str, schema: dict) -> dict:
        llm = self._llm()
        try:
            model = llm.get_model(self.model or self.provider)
            resp = model.prompt(prompt, schema=schema)
            import json

            return json.loads(resp.text())
        except Exception as e:  # noqa: BLE001 -- normalize any llm/provider failure
            raise ModelError(f"api backend failed: {e}") from e
```

(Exact `llm` API surface — `get_model`, `prompt(..., schema=...)`, `.text()` — is verified against the installed `llm` during implementation; adapt names if the library differs. Tests mock `_llm()`, so they don't need `llm` installed.)

- [ ] **Step 7: `models/__init__.py`**

```python
from __future__ import annotations

from .api import ApiModel
from .base import Model, ModelError
from .claude_cli import ClaudeCliModel
from .codex_cli import CodexCliModel

# agent-CLI auto-pick order
MODELS = [ClaudeCliModel, CodexCliModel]

__all__ = ["Model", "ModelError", "ClaudeCliModel", "CodexCliModel", "ApiModel", "active_model", "MODELS"]


def active_model(cfg) -> Model | None:
    backend = getattr(cfg, "backend", None)
    model_name = getattr(cfg, "model", None) or None
    if backend == "api":
        m = ApiModel(provider=getattr(cfg, "api_provider", "anthropic"), model=model_name)
        return m if m.available() else None
    if backend in ("claude", "codex"):
        m = {"claude": ClaudeCliModel, "codex": CodexCliModel}[backend](model=model_name)
        return m if m.available() else None
    # unset -> auto-pick first available agent backend
    for cls in MODELS:
        m = cls(model=model_name)
        if m.available():
            return m
    return None
```

- [ ] **Step 8: `pyproject.toml` — optional extra**

Add after the `dependencies = []` line:

```toml
[project.optional-dependencies]
api = ["llm"]
```

- [ ] **Step 9: Run tests + ruff**

Run: `uv run pytest tests/test_models.py -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean.

- [ ] **Step 10: Commit**

```bash
git add ringleader/models pyproject.toml tests/test_models.py ringleader/config.py
git commit -m "feat: add a Model seam with claude, codex, and api backends"
```

---

### Task 2: Config + `rl config`

**Files:**
- Create: `ringleader/config.py` (finalize if stubbed in Task 1), `ringleader/commands/config.py`
- Modify: `ringleader/paths.py`, `ringleader/commands/__init__.py`
- Test: `tests/test_config.py`, `tests/test_rl_config.py`

**Interfaces:**
- Consumes: `paths.state_dir()`, `models.active_model` (Task 1).
- Produces: `config.Config` (`backend: str | None`, `model: str | None`, `api_provider: str = "anthropic"`), `config.load_config(path=None) -> Config`, `config.save_config(cfg, path=None)`, `paths.config_path() -> Path`; `commands.config.cmd_config(argv) -> int`.

- [ ] **Step 1: Write failing tests**

`tests/test_config.py`:

```python
def test_config_roundtrip(tmp_path):
    from ringleader.config import Config, load_config, save_config

    p = tmp_path / "config.json"
    assert load_config(p) == Config()  # defaults when absent
    save_config(Config(backend="codex", model="gpt-x"), p)
    got = load_config(p)
    assert got.backend == "codex" and got.model == "gpt-x"
```

`tests/test_rl_config.py` (subprocess style, `python -m ringleader`, isolate config via env — see below):

```python
# rl config set backend api ; rl config set api.provider openai ; rl config show
# assert the config file reflects the sets and `show` prints backend=api, provider=openai
```

`rl config` must read/write the config at `paths.config_path()`, which derives from `state_dir()` (`XDG_DATA_HOME`); tests set `XDG_DATA_HOME` to a tmp dir so they never touch the real config.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config.py tests/test_rl_config.py -q`
Expected: FAIL.

- [ ] **Step 3: `paths.config_path()`**

In `ringleader/paths.py`, add:

```python
def config_path() -> Path:
    return state_dir() / "config.json"
```

- [ ] **Step 4: `ringleader/config.py`**

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import config_path


@dataclass
class Config:
    backend: str | None = None
    model: str | None = None
    api_provider: str = "anthropic"


def load_config(path: Path | None = None) -> Config:
    p = Path(path) if path is not None else config_path()
    if not p.exists():
        return Config()
    data = json.loads(p.read_text(encoding="utf-8"))
    return Config(
        backend=data.get("backend"),
        model=data.get("model"),
        api_provider=data.get("api_provider", "anthropic"),
    )


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = Path(path) if path is not None else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
```

- [ ] **Step 5: `commands/config.py`**

`cmd_config(argv)` supporting `show` and `set <key> <value>` where key ∈ `{backend, model, api.provider}`:
- `show`: load config, print `backend`/`model`/`api.provider`, then resolve `active_model(cfg)` and print its `name` + `available()` (or "none — no model configured/available").
- `set backend <claude|codex|api>` / `set model <name>` / `set api.provider <name>`: load, mutate, `save_config`. Reject unknown keys / invalid backend values with a message + exit 2.

- [ ] **Step 6: Wire `rl config` into the dispatch**

In `ringleader/commands/__init__.py`, replace the top-level `"config": _reserved("config")` entry with `"config": config.cmd_config` (import the module).

- [ ] **Step 7: Run tests + ruff**

Run: `uv run pytest tests/test_config.py tests/test_rl_config.py -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean.

- [ ] **Step 8: Commit**

```bash
git add ringleader/config.py ringleader/paths.py ringleader/commands/config.py ringleader/commands/__init__.py tests/test_config.py tests/test_rl_config.py
git commit -m "feat: add rl config for selecting the model backend"
```

---

### Task 3: `rl session enrich` + docs

**Files:**
- Create: `ringleader/commands/enrich.py`
- Modify: `ringleader/commands/__init__.py`, `README.md`, `AGENTS.md`
- Test: `tests/test_rl_enrich.py`

**Interfaces:**
- Consumes: `models.active_model` (Task 1), `config.load_config` (Task 2), `scanner.scan`/`active_sources`, `registry.reconcile`/`sessions_needing_classification`/`apply_classification`/`load_registry`/`save_registry`.

- [ ] **Step 1: Write failing tests**

`tests/test_rl_enrich.py` — invoke `cmd_enrich` directly (not subprocess) so the model + scan can be monkeypatched:

```python
from ringleader.commands import enrich as enrich_mod
from ringleader.models.base import ModelError
from ringleader.sources.base import NormalizedSession


class FakeModel:
    name = "fake"

    def __init__(self, out=None, raise_on=None):
        self._out = out or {"status": "finished", "whatItIs": "did a thing", "suggestedName": "did-a-thing"}
        self._raise_on = raise_on or set()

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        if any(k in prompt for k in self._raise_on):
            raise ModelError("boom")
        return self._out


def _ns(sid, cwd="/p", tail="hello world"):
    return NormalizedSession(
        source="claude-code", session_id=sid, cwd=cwd, mtime="2026-01-01T00:00:00+00:00",
        size_bytes=10, custom_title=None, summary=None, last_prompt="hi", file=f"/p/{sid}.jsonl",
        message_count=3, tail_text=tail,
    )


def test_enrich_classifies_pending(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kw: [_ns("s1")])
    monkeypatch.setattr(enrich_mod.scanner, "active_sources", lambda: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg: FakeModel())
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0
    import json

    t = json.loads(reg.read_text())["threads"][0]
    assert t["status"] == "finished" and t["suggestedName"] == "did-a-thing"


def test_enrich_no_model_is_noop(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kw: [_ns("s1")])
    monkeypatch.setattr(enrich_mod.scanner, "active_sources", lambda: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg: None)
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0
    assert "no model configured" in capsys.readouterr().out.lower()


def test_enrich_skips_on_model_error(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kw: [_ns("s1", tail="EXPLODE now")])
    monkeypatch.setattr(enrich_mod.scanner, "active_sources", lambda: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg: FakeModel(raise_on={"EXPLODE"}))
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0
    import json

    assert json.loads(reg.read_text())["threads"][0]["status"] == "unclassified"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_rl_enrich.py -q`
Expected: FAIL (`ringleader.commands.enrich` missing).

- [ ] **Step 3: `commands/enrich.py`**

```python
from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import projects, registry, scanner
from ..config import load_config
from ..models import active_model
from ..models.base import ModelError
from . import default_registry

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "unfinished", "blocked", "finished"]},
        "whatItIs": {"type": "string"},
        "suggestedName": {"type": "string"},
    },
    "required": ["status", "whatItIs", "suggestedName"],
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prompt(thread: dict, tail: str) -> str:
    return (
        "You are classifying a DORMANT coding-agent session for a curation tool.\n"
        f"project: {thread.get('project')}\n"
        f"git branch: {thread.get('gitBranch')}\n"
        f"last prompt: {thread.get('lastPrompt')}\n"
        f"message count: {thread.get('messageCount')}\n\n"
        "Recent transcript tail:\n"
        f"{tail[:6000]}\n\n"
        "Return: status (active|unfinished|blocked|finished), whatItIs (one line), "
        "suggestedName (short kebab-case)."
    )


def cmd_enrich(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session enrich")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--project", action="append", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    model = active_model(load_config())
    if model is None:
        print("no model configured — set one with `rl config`, or classify via the session-sync skill")
        return 0

    now = _now()
    reg = registry.load_registry(args.registry)
    scanned = scanner.scan(sources=scanner.active_sources())
    tails = {ns.session_id: ns.tail_text for ns in scanned}
    reg = registry.reconcile(reg, scanned, now)

    pending = registry.sessions_needing_classification(reg)
    if args.project:
        pending = projects.filter_by_projects(pending, args.project)
    if args.limit is not None:
        pending = pending[: args.limit]

    done = skipped = 0
    for t in pending:
        try:
            out = model.complete_json(_prompt(t, tails.get(t["sessionId"], "")), ENRICH_SCHEMA)
            registry.apply_classification(
                reg, t["sessionId"], status=out["status"], what_it_is=out["whatItIs"],
                suggested_name=out["suggestedName"],
            )
            done += 1
        except (ModelError, KeyError) as e:
            print(f"skipped {t['sessionId']}: {e}", file=__import__("sys").stderr)
            skipped += 1
    registry.save_registry(args.registry, reg)
    print(f"enriched {done}, skipped {skipped} (model: {model.name})")
    return 0
```

- [ ] **Step 4: Wire `rl session enrich`**

In `ringleader/commands/__init__.py`, replace `_SESSION["enrich"]` value `_reserved("session enrich")` with `enrich.cmd_enrich` (import the module).

- [ ] **Step 5: Run tests + ruff**

Run: `uv run pytest tests/test_rl_enrich.py -q && uv run ruff check && uv run ruff format --check`
Expected: PASS / clean.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -q`
Expected: PASS (no regressions; `session enrich` no longer returns the reserved exit 2 — update any dispatch test that asserted `session enrich` → exit 2, if present, to reflect it now runs).

- [ ] **Step 7: Docs**

- `README.md`: add `rl config` (backend selection, no secrets) and `rl session enrich` (tool-native classify/name; auto-picks an installed agent CLI; `[api]` extra for the `llm` backend) to the command surface; note `session complete` is still reserved.
- `AGENTS.md`: add `rl config` + `rl session enrich` to the `## Commands` block; one line that enrich is the tool-native alternative to the `session-sync` skill (which remains the fallback).

- [ ] **Step 8: Commit**

```bash
git add ringleader/commands/enrich.py ringleader/commands/__init__.py README.md AGENTS.md tests/test_rl_enrich.py
git commit -m "feat: add rl session enrich for tool-native classification"
```

---

## Self-Review

**Spec coverage:**
- Model seam + 3 backends → Task 1. ✓
- No secrets; agent backends reuse auth; api delegates to `llm` → Task 1 (`ApiModel` lazy import, no key stored). ✓
- `llm` optional extra → Task 1 Step 8. ✓
- Zero-config auto-pick (claude→codex) → Task 1 `active_model`. ✓
- `rl config` (non-secret, show/set) → Task 2. ✓
- `rl session enrich` (scan → model → apply_classification, `--project`/`--limit`) → Task 3. ✓
- Graceful degradation → Task 3 (`active_model` None → no-op message). ✓
- Same fields as skill; skill not deleted → Task 3 (uses `apply_classification`; docs note the fallback). ✓
- Tests never call real models (mock subprocess/`llm`/scan/model) → all task tests. ✓
- Verify external CLI flags → Task 1 Steps 4-6. ✓

**Placeholder scan:** No TBD/TODO. The one deliberate deferral — exact `claude`/`codex`/`llm` flag/API names — is a *verification* step (against installed `--help`), with concrete starting commands given; not a placeholder.

**Type consistency:** `Model.complete_json(prompt: str, schema: dict) -> dict` used identically by all backends and by enrich; `active_model(cfg) -> Model | None` consumed by both `rl config show` and enrich; `Config(backend, model, api_provider)` fields read consistently in `active_model` and `load_config`; `apply_classification(..., status=, what_it_is=, suggested_name=)` matches registry.py:170.

## Out of scope

- Harvest / `session complete` / the Target seam — Spec C2.
- Deleting the `session-sync` skill.
- Secret storage in ringleader.
- Additional agent backends (pi, gemini) — the seam stays open to them.
