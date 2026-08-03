# LLM Foundation + Enrich — Design

**Goal:** Give ringleader a tool-native LLM capability — a configurable **Model seam**
and `rl session enrich` that classifies/names/summarizes dormant sessions by calling a
model directly, so classification no longer requires an agent session.

**Architecture:** A `Model` seam (parallel to the `Source` seam) abstracts "given a
prompt + JSON schema, return structured output." Three backends implement it — two
agent-CLI backends (`claude`, `codex`, invoked one-shot, reusing existing auth) and one
API backend (the `llm` library, API-key based). `rl config` selects the backend
(non-secret). `rl session enrich` scans for pending sessions, calls the configured model
per session, and writes the classification back to the registry. When no model is
available it degrades gracefully and the `session-sync` skill remains the fallback.

**Tech Stack:** Python ≥3.13; stdlib `subprocess`/`json` for the agent-CLI backends; the
`llm` library for the API backend (optional extra `ringleader[api]`); `pytest`, `ruff`.

## Scope

This is **Spec C1**, the foundation of the LLM capability. It is the first of two:

- **Spec C1 (this doc):** the Model seam + backends, `rl config`, and `rl session enrich`
  (classify/name/summarize).
- **Spec C2 (later):** the Target seam + `rl session harvest`/`rl session complete`
  (extract a session's knowledge into repo docs, then archive). Builds on C1's config +
  Model seam.

`complete`/`harvest` and the Target seam are **out of scope** here; the reserved verbs
`session enrich`/`session complete`/`config` from the API redesign (Spec 1) exist as
"coming soon" stubs — this spec implements `config` and `session enrich` and leaves
`session complete` reserved.

## Decisions

- **The Model seam mirrors the Source seam.** A family of hand-rolled backends (one per
  agent CLI) plus an API backend, each satisfying one uniform `complete_json` contract.
  We hand-roll (not a unifying library) for the same reasons we hand-rolled sources:
  control over structured output and no dependency on a young library.
- **ringleader stores no secrets.** The agent-CLI backends reuse Claude Code / Codex
  auth; the API backend delegates key management to the `llm` library (`llm keys set`).
  `rl config` holds only non-secret selections.
- **Uniform contract:** `complete_json(prompt, schema) -> dict`. ringleader always
  assembles the context (the agent backends are constrained to answer-only for enrich —
  their file-reading power is a C2/harvest concern).
- **`llm` is an optional extra** (`ringleader[api]`). The agent-CLI backends need no new
  runtime dependency; the API backend is fully built and tested, and installing the extra
  unlocks it. Not dependency dogma — a packaging pragmatism.
- **Zero-config default:** `active_model()` auto-picks the first available agent backend
  (claude → codex) when config names none, so enrich "just works" if an agent CLI is
  installed. `rl config` overrides.
- **Graceful degradation:** no available model → `enrich` does nothing and points at
  `rl config` / the `session-sync` skill. The skill is not deleted; enrich fills the same
  registry fields, so the two paths are interchangeable.

## Components

### 1. Model seam (`ringleader/models/`)

`ringleader/models/base.py` — the protocol + shared types:

```python
@runtime_checkable
class Model(Protocol):
    name: str
    def available(self) -> bool: ...          # backend usable on this machine?
    def complete_json(self, prompt: str, schema: dict) -> dict: ...  # structured output
```

`complete_json` returns the parsed JSON object validated against `schema` (the backend
enforces the schema; on malformed/empty output it raises `ModelError`).

**Backends:**

- `ringleader/models/claude_cli.py` — `ClaudeCliModel(name="claude")`. Writes `schema`
  to a temp file, runs `claude -p --output-format json --json-schema <file>` with the
  prompt on argv/stdin, constrained to answer-only (no tool use, non-interactive), parses
  the `result` field of the JSON envelope, then parses that as the schema-shaped object.
  `available()` = `shutil.which("claude") is not None`.
- `ringleader/models/codex_cli.py` — `CodexCliModel(name="codex")`. Runs
  `codex exec --output-schema <file> <prompt>`, parses the schema-conformant JSON from
  stdout. `available()` = `shutil.which("codex") is not None`.
- `ringleader/models/api.py` — `ApiModel(name="api", provider, model)`. Imports `llm`
  lazily; uses `llm`'s schema support to get structured output for the configured
  provider/model. `available()` = `llm` importable **and** a key configured for the
  provider. Raises a clear error if `ringleader[api]` isn't installed.

`ringleader/models/__init__.py` — the registry + resolver:

```python
def active_model(cfg: Config) -> Model | None:
    # explicit backend from cfg if set and available; else first available agent
    # backend in (ClaudeCliModel, CodexCliModel); else None.
```

### 2. Config (`ringleader/config.py` + `ringleader/commands/config.py`)

A non-secret config file at `state_dir() / "config.toml"` (stdlib `tomllib` to read;
write with a tiny serializer or `json` — decided in the plan). Shape:

```toml
backend = "claude"   # "claude" | "codex" | "api"  (unset -> auto-pick)
model = ""           # optional model override passed to the backend
[api]
provider = "anthropic"   # only when backend = "api"
```

`ringleader/config.py`: `Config` dataclass, `load_config()` (returns defaults if absent),
`save_config()`, `config_path()`.

`rl config` (`commands/config.py`, replacing the reserved stub):
- `rl config show` — print the current config (and which model `active_model` resolves to,
  plus its availability).
- `rl config set <key> <value>` — set `backend` / `model` / `api.provider`.

### 3. Enrich (`ringleader/commands/enrich.py`)

`rl session enrich [--project <sel>] [--limit N] [--registry <path>]` (replaces the
reserved `session enrich` stub):

1. Resolve `model = active_model(load_config())`. If `None` → print the degradation
   message and return 0 (no-op).
2. Scan `active_sources()` (like `refresh`) to reconcile the registry and obtain the
   pending-classification sessions **with their `tail_text`** (tail is transient, only
   available from a fresh parse).
3. Filter by `--project`; cap by `--limit`.
4. For each pending session: build the prompt from its `tail_text` + metadata (project,
   git branch, last prompt, message count), call
   `model.complete_json(prompt, ENRICH_SCHEMA)`, and apply via
   `registry.apply_classification(reg, session_id, status=…, what_it_is=…,
   suggested_name=…)`. On a per-session `ModelError`, print a skip line to stderr and
   continue.
5. Save the registry; print a summary (`classified N, skipped M`).

`ENRICH_SCHEMA` (module constant):

```python
ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "unfinished", "blocked", "finished"]},
        "whatItIs": {"type": "string"},
        "suggestedName": {"type": "string"},
    },
    "required": ["status", "whatItIs", "suggestedName"],
    "additionalProperties": False,
}
```

The prompt instructs: classify this dormant coding session's status, a one-line
"what it is", and a short kebab-case name, given the recent transcript tail + metadata.

### 4. Relationship to the `session-sync` skill

The skill's flow is `refresh` (emit pending + tails) → agent judges → `set` (apply). Enrich
collapses that into one tool-native command by substituting the model for the agent's
judgment. The skill stays as the fallback for when no model is configured or when
full-agent judgment is wanted; both write the same `status`/`whatItIs`/`suggestedName`
fields, so a registry can be enriched by either path interchangeably.

## Error handling

- `ModelError` (in `models/base.py`) for empty/malformed/schema-violating backend output
  and backend invocation failures (non-zero exit, missing CLI mid-run). Enrich catches it
  per session (skip + continue); `rl config show` surfaces backend availability.
- Missing `ringleader[api]` when `backend = "api"` → a clear "install ringleader[api]"
  message, treated as an unavailable model (degradation), not a crash.

## Testing

- **No real model calls, ever.** Agent-CLI backends: mock `subprocess.run` and assert the
  exact command/args/schema-file are constructed, and that a canned JSON envelope parses
  correctly (including the `claude` `.result` unwrap). API backend: mock the `llm` call.
- **`active_model` resolution:** explicit backend honored; auto-pick order (claude→codex)
  when unset; `None` when nothing available; api unavailable when `llm`/key absent.
- **Enrich with a fake `Model`** (returns canned structured output): asserts the right
  `status`/`whatItIs`/`suggestedName` land on the right session via `apply_classification`;
  `--project`/`--limit` scope correctly; per-session `ModelError` skips and continues.
- **Graceful degradation:** enrich with no model configured/available is a no-op with the
  guidance message (exit 0).
- **Config round-trip:** `load_config`/`save_config`; `rl config set`/`show`.
- **Test isolation:** enrich tests seed a tmp registry and either inject explicit
  `sources=`/a fake scanner or isolate `SESSION_TRACKER_PROJECTS_DIR`/`CODEX_SESSIONS_DIR`
  so no test scans real `~/.claude`/`~/.codex` (per the live-data discipline from the
  Codex-source work).

## Out of scope

- **Harvest / `session complete` / the Target seam** — Spec C2.
- **Deleting the `session-sync` skill** — it remains the agent fallback.
- **Secret storage in ringleader** — delegated to Claude Code / `llm`.
- **A unifying agent-CLI library** — hand-rolled per-CLI backends instead; the seam stays
  open to more agent backends (pi, gemini, …) later, added like Sources.
